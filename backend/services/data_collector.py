"""
Continuous Binance WebSocket collector.

Writes 1-per-second snapshots to `market_ticks` for every tracked USDT pair
(including BTC/USDT itself) with:
  - Lead-lag features: BTC ROC at multiple windows driving alt price changes
  - Volume delta: actual per-second volume change (not a 24h snapshot)
  - Extended features_json: richer indicator blob for ML training
  - Execution mode: follows app_settings (same source of truth as bot_runner)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import deque

import websockets
from dotenv import load_dotenv

# ── Path / env setup ───────────────────────────────────────────────────────
_svc_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_svc_dir)
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))

if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)

from database import get_db_connection
from tracked_markets import (
    BTC_STREAM_ID,
    TRACKED_USDT_STREAM_IDS,
    alt_stream_ids,
    stream_id_to_ccxt,
)
from trading.app_settings import get_execution_mode

# ── Constants ──────────────────────────────────────────────────────────────

# Price history length in seconds (also caps deque memory).
PRICE_HISTORY_SEC = 120

# ROC windows to compute (both for main columns and extended features_json).
ROC_WINDOWS = (1, 5, 10, 30, 60)

WS_RECONNECT_BASE_SEC = 2.0
WS_RECONNECT_MAX_SEC = 60.0

# ── Per-symbol state ────────────────────────────────────────────────────────

_AssetState = dict  # type alias for readability

state: dict[str, _AssetState] = {}
for _sym in TRACKED_USDT_STREAM_IDS:
    state[_sym] = {
        "price": deque(maxlen=PRICE_HISTORY_SEC),
        "volume_24h": 0.0,       # latest 24h rolling volume from @ticker
        "prev_volume_24h": None, # previous snapshot for delta calculation
        "volume_delta": 0.0,     # per-second volume change (computed each cycle)
        "latest_price": 0.0,
        "bid": 0.0,
        "ask": 0.0,
        "spread_bps": 0.0,
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_roc(price_history: deque, seconds: int) -> float:
    """Rate of change over `seconds` periods (each period = 1 s)."""
    if len(price_history) < seconds + 1:
        return 0.0
    current = price_history[-1]
    past = price_history[-(seconds + 1)]
    if past == 0:
        return 0.0
    return (current - past) / past


def _roc_dict(stream_id: str, prefix: str) -> dict[str, float]:
    """Build {prefix_roc_Xs: value} for all ROC_WINDOWS."""
    hist = state[stream_id]["price"]
    return {f"{prefix}_roc_{w}s": _get_roc(hist, w) for w in ROC_WINDOWS}


# ── WebSocket listener ──────────────────────────────────────────────────────

async def _binance_ws_listener() -> None:
    streams: list[str] = []
    for sym in TRACKED_USDT_STREAM_IDS:
        streams.append(f"{sym}@ticker")
        streams.append(f"{sym}@bookTicker")

    stream_path = "/".join(streams)
    reconnect_delay = WS_RECONNECT_BASE_SEC

    while True:
        mode = get_execution_mode()
        base_url = (
            "wss://stream.testnet.binance.vision"
            if mode != "live"
            else "wss://stream.binance.com:9443"
        )
        ws_url = f"{base_url}/stream?streams={stream_path}"
        print(f"[data_collector] Connecting to {base_url} (mode={mode})")

        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=60) as ws:
                print("[data_collector] WebSocket connected.")
                reconnect_delay = WS_RECONNECT_BASE_SEC  # reset on success
                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    if "data" not in data or "stream" not in data:
                        continue

                    stream_name: str = data["stream"]
                    payload: dict = data["data"]
                    symbol = payload.get("s", "").lower()

                    if not symbol or symbol not in state:
                        continue

                    if stream_name.endswith("@ticker"):
                        state[symbol]["latest_price"] = float(payload.get("c", 0) or 0)
                        state[symbol]["volume_24h"] = float(payload.get("v", 0) or 0)

                    elif stream_name.endswith("@bookTicker"):
                        bid = float(payload.get("b", 0) or 0)
                        ask = float(payload.get("a", 0) or 0)
                        state[symbol]["bid"] = bid
                        state[symbol]["ask"] = ask
                        if ask > 0 and bid > 0:
                            mid = (ask + bid) / 2
                            state[symbol]["spread_bps"] = round(((ask - bid) / mid) * 10_000, 2)

        except Exception as exc:
            print(f"[data_collector] WS error: {exc}")
            print(f"[data_collector] Reconnecting in {reconnect_delay:.0f}s …")
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, WS_RECONNECT_MAX_SEC)


# ── DB writer ───────────────────────────────────────────────────────────────

def _compute_volume_delta(sym: str) -> float:
    """
    Compute per-second volume delta from the 24h rolling ticker volume.
    On the first tick the delta is 0.
    """
    current = state[sym]["volume_24h"]
    prev = state[sym]["prev_volume_24h"]
    if prev is None:
        state[sym]["prev_volume_24h"] = current
        return 0.0
    delta = max(0.0, current - prev)   # 24h rolling can only grow or reset at midnight
    state[sym]["prev_volume_24h"] = current
    state[sym]["volume_delta"] = delta
    return delta


def _write_ticks(timestamp: int) -> None:
    """Snapshot every tracked symbol into market_ticks (including BTC)."""
    btc = state[BTC_STREAM_ID]
    btc_price = btc["price"][-1] if btc["price"] else 0.0
    if btc_price == 0.0:
        return  # wait until BTC is live

    btc_vol_delta = _compute_volume_delta(BTC_STREAM_ID)
    btc_roc = _roc_dict(BTC_STREAM_ID, "btc")

    rows: list[tuple] = []

    for sym in TRACKED_USDT_STREAM_IDS:
        asset_price = state[sym]["price"][-1] if state[sym]["price"] else 0.0
        if asset_price == 0.0:
            continue

        vol_delta = _compute_volume_delta(sym) if sym != BTC_STREAM_ID else btc_vol_delta
        target_roc = _roc_dict(sym, "target")

        features: dict = {
            # Full bid/ask
            "bid": state[sym]["bid"],
            "ask": state[sym]["ask"],
            "btc_bid": btc["bid"],
            "btc_ask": btc["ask"],
            "btc_spread_bps": btc["spread_bps"],
            # Extended ROC windows for ML
            **{f"target_roc_{w}s": target_roc[f"target_roc_{w}s"] for w in ROC_WINDOWS},
            **{f"btc_roc_{w}s": btc_roc[f"btc_roc_{w}s"] for w in ROC_WINDOWS},
        }

        rows.append((
            timestamp,
            stream_id_to_ccxt(sym),
            asset_price,
            btc_price,
            btc_roc["btc_roc_1s"],
            btc_roc["btc_roc_5s"],
            target_roc["target_roc_1s"],
            target_roc["target_roc_5s"],
            btc_vol_delta,
            vol_delta,
            state[sym]["spread_bps"],
            json.dumps(features),
        ))

    if not rows:
        return

    conn = get_db_connection()
    try:
        conn.executemany(
            """
            INSERT INTO market_ticks
              (timestamp, target_asset, target_price, btc_price,
               btc_roc_1s, btc_roc_5s, target_roc_1s, target_roc_5s,
               btc_volume_delta, target_volume_delta, spread_bps, features_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


async def _data_logger() -> None:
    print("[data_collector] Waiting 5s for initial stream data …")
    await asyncio.sleep(5)

    while True:
        try:
            timestamp = int(time.time() * 1000)

            # Snapshot current prices into rolling deques
            for sym in TRACKED_USDT_STREAM_IDS:
                state[sym]["price"].append(state[sym]["latest_price"])

            _write_ticks(timestamp)

        except Exception as exc:
            print(f"[data_collector] Logger error: {exc}")

        await asyncio.sleep(1.0)


# ── Entry point ─────────────────────────────────────────────────────────────

async def main() -> None:
    print(
        f"[data_collector] Starting — tracking {len(TRACKED_USDT_STREAM_IDS)} pairs: "
        + ", ".join(TRACKED_USDT_STREAM_IDS)
    )
    await asyncio.gather(
        _binance_ws_listener(),
        _data_logger(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[data_collector] Stopped.")
