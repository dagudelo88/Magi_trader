import sys
import os
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel

from database import (
    get_db_connection,
    init_db,
    fetch_bot_orders_panel,
    sync_bot_orders_from_logs,
)
from tracked_markets import (
    TRACKED_BASE_ORDER,
    TRACKED_USDT_STREAM_IDS,
    stream_id_to_ticker_symbol,
    TRACKED_CCXT_SYMBOLS,
    wallet_assets_allowed,
)
from trading.app_settings import (
    apply_execution_mode,
    get_execution_mode,
    is_global_halt,
    set_global_halt,
    trading_settings_snapshot,
)
from trading.constants import LIVE_TRADING_CONFIRMATION_PHRASE
from trading.binance_errors import explain_fetch_balance_error
from trading.exchange_factory import build_binance_spot

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="MagiTrader API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

collector_process = None
bot_runner_process = None


def ensure_bot_runner() -> None:
    global bot_runner_process
    script_path = os.path.join(_backend_dir, "services", "bot_runner.py")
    if bot_runner_process is None or bot_runner_process.poll() is not None:
        bot_runner_process = subprocess.Popen([sys.executable, script_path])


def get_exchange_authenticated():
    mode = get_execution_mode()
    try:
        return build_binance_spot(mode)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/wallet/balances")
def get_wallet_balances(
    view: str | None = Query(
        default=None,
        description="testnet | live — which network to query. Omit to use Settings execution_mode.",
    ),
):
    bot_mode = get_execution_mode()
    if view is not None and view not in ("testnet", "live"):
        raise HTTPException(
            status_code=400,
            detail="Query 'view' must be 'testnet' or 'live'.",
        )
    effective = view if view in ("testnet", "live") else bot_mode

    try:
        exchange = build_binance_spot(effective)
        balance = exchange.fetch_balance()

        non_zero_balances = []
        if "total" in balance:
            for asset, amount in balance["total"].items():
                if amount > 0:
                    free_amt = balance.get("free", {}).get(asset, 0)
                    used_amt = balance.get("used", {}).get(asset, 0)
                    non_zero_balances.append(
                        {
                            "asset": asset,
                            "free": free_amt,
                            "used": used_amt,
                            "total": amount,
                        }
                    )

        # Testnet view: same eight bases (+ stables) as the collector WebSocket; mainnet: full wallet.
        if effective == "testnet":
            allowed = wallet_assets_allowed()
            non_zero_balances = [b for b in non_zero_balances if b["asset"] in allowed]

        stable_order = ["USDT", "USDC", "FDUSD", "BUSD"]
        base_pos = {a: i for i, a in enumerate(TRACKED_BASE_ORDER)}
        st_pos = {a: i for i, a in enumerate(stable_order)}

        def _balance_sort_key(row: dict) -> tuple:
            a = row["asset"]
            if a in base_pos:
                return (0, base_pos[a])
            if a in st_pos:
                return (1, st_pos[a])
            return (2, a)

        non_zero_balances.sort(key=_balance_sort_key)
        return {
            "balances": non_zero_balances,
            "wallet_view": effective,
            "execution_mode": bot_mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=explain_fetch_balance_error(e, effective),
        ) from e


@app.get("/api/market/tracked")
def get_tracked_markets():
    """Same eight spot pairs as the backend WebSocket collector and testnet-focused wallet view."""
    return {
        "stream_ids": list(TRACKED_USDT_STREAM_IDS),
        "ticker_symbols": [stream_id_to_ticker_symbol(s) for s in TRACKED_USDT_STREAM_IDS],
        "ccxt_symbols": list(TRACKED_CCXT_SYMBOLS),
    }


@app.get("/api/data/status")
def get_status():
    global collector_process
    is_active = collector_process is not None and collector_process.poll() is None
    return {"active": is_active}


@app.post("/api/data/start")
def start_collection():
    global collector_process
    if collector_process is None or collector_process.poll() is not None:
        script_path = os.path.join(os.path.dirname(__file__), "services", "data_collector.py")
        collector_process = subprocess.Popen([sys.executable, script_path])
    return {"active": True}


@app.post("/api/data/stop")
def stop_collection():
    global collector_process
    if collector_process and collector_process.poll() is None:
        collector_process.terminate()
        collector_process.wait()
        collector_process = None
    return {"active": False}


# --- Trading settings ---


@app.get("/api/settings/trading")
def get_trading_settings():
    snap = trading_settings_snapshot()
    return {
        **snap,
        "live_confirmation_phrase": LIVE_TRADING_CONFIRMATION_PHRASE,
    }


class TradingSettingsBody(BaseModel):
    execution_mode: str
    confirmation_phrase: str | None = None


@app.put("/api/settings/trading")
def put_trading_settings(body: TradingSettingsBody):
    try:
        return apply_execution_mode(body.execution_mode, body.confirmation_phrase)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class HaltBody(BaseModel):
    halted: bool


@app.put("/api/settings/trading/halt")
def put_trading_halt(body: HaltBody):
    set_global_halt(body.halted)
    return trading_settings_snapshot()


# --- Bots ---


def _row_to_bot(row) -> dict:
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


@app.get("/api/bots")
def list_bots():
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bots ORDER BY created_at")
        return {"bots": [_row_to_bot(r) for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get("/api/bots/{bot_id}")
def get_bot(bot_id: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bot not found")
        cur.execute(
            """
            SELECT * FROM bot_logs WHERE bot_id = ?
            ORDER BY created_at DESC LIMIT 150
            """,
            (bot_id,),
        )
        logs = [_row_to_bot(r) for r in cur.fetchall()]
        bot_row = _row_to_bot(row)
        sym = str(bot_row.get("symbol") or "")
        sync_bot_orders_from_logs(bot_id, sym)
        order_stats, orders = fetch_bot_orders_panel(bot_id, order_limit=50)
        return {
            "bot": bot_row,
            "logs": logs,
            "execution_mode": get_execution_mode(),
            "order_stats": order_stats,
            "orders": orders,
        }
    finally:
        conn.close()


class BotStatusBody(BaseModel):
    status: str


@app.put("/api/bots/{bot_id}/status")
def set_bot_status(bot_id: str, body: BotStatusBody):
    if body.status not in ("running", "stopped", "paused"):
        raise HTTPException(status_code=400, detail="status must be running, stopped, or paused")
    if body.status == "running" and is_global_halt():
        raise HTTPException(
            status_code=409,
            detail="Global trading halt is ON — turn it off in Settings before running bots.",
        )

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT bot_id FROM bots WHERE bot_id = ?", (bot_id,))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        cur.execute(
            "UPDATE bots SET status = ? WHERE bot_id = ?",
            (body.status, bot_id),
        )
        conn.commit()
    finally:
        conn.close()

    if body.status == "running":
        ensure_bot_runner()

    return {
        "bot_id": bot_id,
        "status": body.status,
        "runner_ensured": body.status == "running",
    }
