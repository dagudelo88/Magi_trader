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
import logging
import os
import random
import sys
import time
from collections import deque

_dc_logger = logging.getLogger("data_collector")

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
from services.websocket_manager import publish_market_event
# execution_mode is intentionally NOT used for data collection —
# we always stream from mainnet for real market microstructure.

# ── Constants ──────────────────────────────────────────────────────────────

# Price history length in seconds (also caps deque memory).
PRICE_HISTORY_SEC = 120

# ROC windows to compute (both for main columns and extended features_json).
ROC_WINDOWS = (1, 5, 10, 30, 60)

WS_RECONNECT_BASE_SEC = 2.0
WS_RECONNECT_MAX_SEC = 60.0

# Binance Spot WebSocket Streams compliance:
# - Combined streams use /stream?streams=...
# - Max 1024 streams per connection.
# - We do not send live SUBSCRIBE/UNSUBSCRIBE control messages, keeping well
#   under the 5 incoming control messages/second limit. The websockets client
#   automatically returns pong frames with the ping payload; client-initiated
#   pings are disabled so server ping/pong drives liveness.
# - Binance disconnects connections at 24 h, so each connection recycles
#   proactively before that hard limit.
BINANCE_MAX_STREAMS_PER_CONNECTION = 1024
BINANCE_PROACTIVE_RECONNECT_SEC = 23 * 60 * 60 + 50 * 60
BINANCE_RECV_TIMEOUT_SEC = 30.0
MARKET_BROADCAST_MIN_INTERVAL_SEC = 2.0

# Batch market_ticks writes: accumulate N seconds of rows before opening a
# DB connection.  Reduces SQLite write pressure from 1 connection/s to
# 1 connection / TICK_BATCH_SEC without sacrificing time-series granularity.
TICK_BATCH_SEC: int = int(os.environ.get("TICK_BATCH_SEC", "5"))

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

_last_market_broadcast: dict[str, float] = {}


class _ServerShutdown(Exception):
    """Raised when Binance sends the documented serverShutdown event."""


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


def _chunk_streams(streams: list[str]) -> list[list[str]]:
    return [
        streams[i : i + BINANCE_MAX_STREAMS_PER_CONNECTION]
        for i in range(0, len(streams), BINANCE_MAX_STREAMS_PER_CONNECTION)
    ]


def _is_server_shutdown(stream_name: str | None, payload: dict) -> bool:
    return stream_name == "!serverShutdown" or payload.get("e") == "serverShutdown"


def _publish_market_tick(symbol: str, payload: dict) -> None:
    now = time.monotonic()
    if now - _last_market_broadcast.get(symbol, 0.0) < MARKET_BROADCAST_MIN_INTERVAL_SEC:
        return
    _last_market_broadcast[symbol] = now
    publish_market_event(
        "market_tick",
        {
            "symbol": stream_id_to_ccxt(symbol),
            "stream_id": symbol,
            "last_price": float(payload.get("c", 0) or 0),
            "price_change": float(payload.get("p", 0) or 0),
            "price_change_percent": float(payload.get("P", 0) or 0),
            "volume_24h": float(payload.get("v", 0) or 0),
            "event_time": int(payload.get("E", 0) or 0),
        },
    )


# ── WebSocket listener ──────────────────────────────────────────────────────

async def _binance_ws_connection(streams: list[str], connection_id: int) -> None:
    reconnect_delay = WS_RECONNECT_BASE_SEC

    # Always use mainnet for price data — public streams need no API key and
    # provide real market microstructure regardless of bot execution_mode.
    base_url = "wss://stream.binance.com:9443"
    stream_path = "/".join(streams)
    ws_url = f"{base_url}/stream?streams={stream_path}"

    while True:
        print(
            f"[data_collector] Connecting Binance WS #{connection_id} "
            f"({len(streams)} combined streams, mainnet real prices)"
        )

        try:
            async with websockets.connect(
                ws_url,
                ping_interval=None,
                ping_timeout=None,
                close_timeout=10,
            ) as ws:
                print(f"[data_collector] Binance WS #{connection_id} connected.")
                reconnect_delay = WS_RECONNECT_BASE_SEC  # reset on success
                connected_at = time.monotonic()
                while time.monotonic() - connected_at < BINANCE_PROACTIVE_RECONNECT_SEC:
                    msg = await asyncio.wait_for(
                        ws.recv(),
                        timeout=BINANCE_RECV_TIMEOUT_SEC,
                    )
                    data = json.loads(msg)

                    if "data" not in data or "stream" not in data:
                        continue

                    stream_name: str = data["stream"]
                    payload: dict = data["data"]
                    if _is_server_shutdown(stream_name, payload):
                        raise _ServerShutdown("Binance serverShutdown event received")

                    symbol = payload.get("s", "").lower()

                    if not symbol or symbol not in state:
                        continue

                    if stream_name.endswith("@ticker"):
                        state[symbol]["latest_price"] = float(payload.get("c", 0) or 0)
                        state[symbol]["volume_24h"] = float(payload.get("v", 0) or 0)
                        _publish_market_tick(symbol, payload)

                    elif stream_name.endswith("@bookTicker"):
                        bid = float(payload.get("b", 0) or 0)
                        ask = float(payload.get("a", 0) or 0)
                        state[symbol]["bid"] = bid
                        state[symbol]["ask"] = ask
                        if ask > 0 and bid > 0:
                            mid = (ask + bid) / 2
                            state[symbol]["spread_bps"] = round(((ask - bid) / mid) * 10_000, 2)

                print(
                    f"[data_collector] Binance WS #{connection_id} proactive reconnect "
                    "before 24h connection limit."
                )
        except _ServerShutdown as exc:
            print(f"[data_collector] {exc} — reconnecting immediately.")
            reconnect_delay = WS_RECONNECT_BASE_SEC
            continue
        except asyncio.TimeoutError:
            print(
                f"[data_collector] Binance WS #{connection_id} timed out waiting for data; "
                "reconnecting."
            )
        except Exception as exc:
            print(f"[data_collector] Binance WS #{connection_id} error: {exc}")

        jitter = random.uniform(0.0, reconnect_delay * 0.2)
        sleep_for = reconnect_delay + jitter
        print(f"[data_collector] Binance WS #{connection_id} reconnecting in {sleep_for:.1f}s …")
        await asyncio.sleep(sleep_for)
        reconnect_delay = min(reconnect_delay * 2, WS_RECONNECT_MAX_SEC)


async def _binance_ws_listener() -> None:
    streams: list[str] = []
    for sym in TRACKED_USDT_STREAM_IDS:
        streams.append(f"{sym}@ticker")
        streams.append(f"{sym}@bookTicker")

    chunks = _chunk_streams(streams)
    await asyncio.gather(
        *(
            _binance_ws_connection(chunk, idx + 1)
            for idx, chunk in enumerate(chunks)
        )
    )


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


def _build_tick_rows(timestamp: int) -> list[tuple]:
    """Compute one second's tick rows for all tracked symbols.

    Returns an empty list when BTC price is not yet available (data not live).
    Does NOT write to the database — call _flush_ticks() to persist.
    """
    btc = state[BTC_STREAM_ID]
    btc_price = btc["price"][-1] if btc["price"] else 0.0
    if btc_price == 0.0:
        return []  # wait until BTC is live

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
            "bid": state[sym]["bid"],
            "ask": state[sym]["ask"],
            "btc_bid": btc["bid"],
            "btc_ask": btc["ask"],
            "btc_spread_bps": btc["spread_bps"],
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

    return rows


def _flush_ticks(pending_rows: list[tuple]) -> None:
    """Write accumulated tick rows to the database in one connection + commit.

    Batching TICK_BATCH_SEC seconds of rows into a single write reduces SQLite
    open/commit/close overhead from once per second to once per batch interval
    without losing any time-series resolution in the stored data.
    """
    if not pending_rows:
        return

    t0 = time.perf_counter()
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
            pending_rows,
        )
        conn.commit()
    except Exception as exc:
        _dc_logger.error("[data_collector] market_ticks flush failed: %s", exc)
    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    try:
        from services.monitoring import monitor  # noqa: PLC0415
        monitor.record_db_op(f"flush_ticks({len(pending_rows)} rows)", elapsed_ms)
    except Exception:
        pass
    if elapsed_ms > 500:
        _dc_logger.warning(
            "[data_collector] SLOW flush_ticks: %d rows → %.0f ms", len(pending_rows), elapsed_ms
        )


async def _data_logger() -> None:
    print(
        f"[data_collector] Waiting 5s for initial stream data … "
        f"(batch interval: {TICK_BATCH_SEC}s)"
    )
    await asyncio.sleep(5)

    pending_rows: list[tuple] = []
    tick_counter: int = 0

    while True:
        try:
            timestamp = int(time.time() * 1000)

            # Snapshot current prices into rolling deques (every second).
            for sym in TRACKED_USDT_STREAM_IDS:
                state[sym]["price"].append(state[sym]["latest_price"])

            # Build rows for this second without touching the DB.
            rows = _build_tick_rows(timestamp)
            pending_rows.extend(rows)
            tick_counter += 1

            # Flush to DB once per TICK_BATCH_SEC seconds.
            if tick_counter >= TICK_BATCH_SEC:
                _flush_ticks(pending_rows)
                pending_rows = []
                tick_counter = 0

        except Exception as exc:
            _dc_logger.error("[data_collector] Logger error: %s", exc)

        await asyncio.sleep(1.0)


# ── Entry points ────────────────────────────────────────────────────────────

async def _main_coro() -> None:
    print("[data_collector] Binance WebSocket Streams: FULLY COMPLIANT")
    print(
        f"[data_collector] Starting — tracking {len(TRACKED_USDT_STREAM_IDS)} pairs: "
        + ", ".join(TRACKED_USDT_STREAM_IDS)
    )
    await asyncio.gather(
        _binance_ws_listener(),
        _data_logger(),
    )


async def run_async() -> None:
    """
    Asyncio entry-point for use as a long-running asyncio.Task inside the
    FastAPI lifespan.  Should NOT be executed via subprocess or asyncio.run().
    """
    try:
        await _main_coro()
    except asyncio.CancelledError:
        print("[data_collector] Task cancelled — shutting down.")
        raise


if __name__ == "__main__":
    try:
        asyncio.run(_main_coro())
    except KeyboardInterrupt:
        print("[data_collector] Stopped.")
