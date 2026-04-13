import json
import sys
import os
import subprocess
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from pydantic import BaseModel

from database import (
    create_bot,
    delete_bot,
    get_db_connection,
    init_db,
    fetch_bot_orders_panel,
    fetch_bot_orders_chronological,
    sync_bot_orders_from_logs,
    refresh_stale_bot_orders_from_exchange,
    fork_bot,
    update_bot,
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
from trading.bot_performance import compute_strategy_performance
from trading.strategy_budget import (
    initial_budget_from_strategy_params_json,
    merge_strategy_params_json,
    parse_initial_budget_api_value,
)

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="MagiTrader API", lifespan=lifespan)


def _cors_allow_origins() -> list[str]:
    """
    Browsers reject Access-Control-Allow-Origin: * when credentials are used.
    Set CORS_ORIGINS=comma-separated URLs (e.g. http://localhost:5000,http://127.0.0.1:5173).
    """
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
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


@app.get("/api/market/ohlcv")
def get_market_ohlcv(
    symbol: str = Query(..., description="CCXT symbol, e.g. BTC/USDT"),
    timeframe: str = Query("5m", description="Binance-style timeframe"),
    limit: int = Query(100, ge=10, le=500),
):
    """Public OHLCV for charts — uses the same execution mode as Settings (testnet vs live)."""
    mode = get_execution_mode()
    try:
        exchange = build_binance_spot(mode)
        if not exchange.markets:
            exchange.load_markets()
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OHLCV fetch failed: {e}") from e

    candles = [
        {"t": int(t), "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v)}
        for t, o, h, l, c, v in ohlcv
    ]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "candles": candles,
        "execution_mode": mode,
        "count": len(candles),
    }


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
        rows = [_row_to_bot(r) for r in cur.fetchall()]
        for row in rows:
            raw_params = row.get("strategy_params_json")
            row["initial_budget_quote"] = initial_budget_from_strategy_params_json(
                raw_params if isinstance(raw_params, str) else None
            )
        return {"bots": rows}
    finally:
        conn.close()


class CreateBotBody(BaseModel):
    name: str
    symbol: str
    strategy: str = "sma_cross"
    initial_budget_quote: float
    strategy_params: dict[str, Any] | None = None


@app.post("/api/bots", status_code=201)
def post_create_bot(body: CreateBotBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")
    if not body.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol must not be empty")
    if body.strategy != "sma_cross":
        raise HTTPException(status_code=400, detail="only sma_cross strategy is supported")
    if body.initial_budget_quote <= 0:
        raise HTTPException(status_code=400, detail="initial_budget_quote must be positive")
    from trading.strategies.sma_cross import default_strategy_params
    params = default_strategy_params()
    if body.strategy_params:
        safe_keys = {
            "fast_period", "slow_period", "quote_fraction", "base_fraction",
            "min_trade_interval_sec", "ohlcv_timeframe", "ohlcv_limit",
        }
        params.update({k: v for k, v in body.strategy_params.items() if k in safe_keys})
    params["initial_budget_quote"] = body.initial_budget_quote
    params_json = json.dumps(params, default=str)
    try:
        bot = create_bot(body.name, body.symbol, body.strategy, params_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"bot": bot}


class UpdateBotBody(BaseModel):
    name: str | None = None
    symbol: str | None = None


@app.patch("/api/bots/{bot_id}")
def patch_bot(bot_id: str, body: UpdateBotBody):
    try:
        bot = update_bot(bot_id, body.name, body.symbol)
    except ValueError as e:
        msg = str(e)
        code = 409 if "running" in msg else 404
        raise HTTPException(status_code=code, detail=msg) from e
    return {"bot": bot}


@app.delete("/api/bots/{bot_id}", status_code=204)
def delete_bot_endpoint(bot_id: str):
    try:
        delete_bot(bot_id)
    except ValueError as e:
        msg = str(e)
        code = 409 if "running" in msg else 404
        raise HTTPException(status_code=code, detail=msg) from e


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
        try:
            refresh_stale_bot_orders_from_exchange(bot_id, build_binance_spot(get_execution_mode()))
        except Exception:
            pass
        order_stats, orders = fetch_bot_orders_panel(bot_id, order_limit=50)
        orders_asc = fetch_bot_orders_chronological(bot_id)
        mark_price: float | None = None
        if sym:
            try:
                ex = build_binance_spot(get_execution_mode())
                if not ex.markets:
                    ex.load_markets()
                tk = ex.fetch_ticker(sym)
                lp = tk.get("last")
                if lp is not None:
                    mark_price = float(lp)
            except Exception:
                mark_price = None
        perf = compute_strategy_performance(orders_asc, sym, mark_price=mark_price)
        total_pnl = perf["realized_pnl_quote"] + (perf["unrealized_pnl_quote"] or 0.0)
        raw_params = bot_row.get("strategy_params_json")
        budget = initial_budget_from_strategy_params_json(
            raw_params if isinstance(raw_params, str) else None
        )
        pnl_vs_budget_pct: float | None = None
        max_dd_vs_budget_pct: float | None = None
        if budget is not None and budget > 0:
            pnl_vs_budget_pct = round((total_pnl / budget) * 100.0, 4)
            max_dd_vs_budget_pct = round((perf["max_drawdown_quote"] / budget) * 100.0, 4)
        current_capital: float | None = None
        if budget is not None and budget > 0:
            current_capital = round(budget + total_pnl, 8)

        strategy_health = {
            "realized_pnl_quote": round(perf["realized_pnl_quote"], 8),
            "unrealized_pnl_quote": (
                round(perf["unrealized_pnl_quote"], 8)
                if perf["unrealized_pnl_quote"] is not None
                else None
            ),
            "open_base_position": round(perf["open_base_position"], 8),
            "open_cost_basis_quote": round(perf["open_cost_basis_quote"], 8),
            "closed_trades": perf["closed_trades"],
            "winning_trades": perf["winning_trades"],
            "losing_trades": perf["losing_trades"],
            "breakeven_trades": perf["breakeven_trades"],
            "win_rate_pct": (
                round(perf["win_rate_pct"], 2) if perf["win_rate_pct"] is not None else None
            ),
            "max_drawdown_quote": round(perf["max_drawdown_quote"], 8),
            "max_drawdown_pct": (
                round(perf["max_drawdown_pct"], 4) if perf["max_drawdown_pct"] is not None else None
            ),
            "quote_currency": perf["quote_currency"],
            "mark_price": round(mark_price, 8) if mark_price is not None else None,
            "total_pnl_quote": round(total_pnl, 8),
            "initial_budget_quote": round(budget, 8) if budget is not None else None,
            "current_capital_quote": current_capital,
            "pnl_return_on_budget_pct": pnl_vs_budget_pct,
            "max_drawdown_vs_budget_pct": max_dd_vs_budget_pct,
        }
        return {
            "bot": bot_row,
            "logs": logs,
            "execution_mode": get_execution_mode(),
            "order_stats": order_stats,
            "orders": orders,
            "strategy_health": strategy_health,
        }
    finally:
        conn.close()


@app.patch("/api/bots/{bot_id}/strategy-params")
def patch_bot_strategy_params(bot_id: str, body: dict[str, Any] = Body(...)):
    if not body:
        raise HTTPException(status_code=400, detail="body must not be empty")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT strategy_params_json FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bot not found")
        existing = row["strategy_params_json"]
        merged = merge_strategy_params_json(existing if isinstance(existing, str) else None, body)
        if "initial_budget_quote" in merged:
            try:
                merged["initial_budget_quote"] = parse_initial_budget_api_value(
                    merged["initial_budget_quote"]
                )
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        out_json = json.dumps(merged, default=str)
        cur.execute(
            "UPDATE bots SET strategy_params_json = ? WHERE bot_id = ?",
            (out_json, bot_id),
        )
        conn.commit()
        return {"strategy_params": merged, "strategy_params_json": out_json}
    finally:
        conn.close()


@app.post("/api/bots/{bot_id}/fork")
def post_fork_bot(bot_id: str, body: dict[str, Any] = Body(default_factory=dict)):
    """Clone bot config to a new bot_id. History (orders, logs) stays on the source bot only."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT strategy_params_json FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bot not found")
        existing_params = row["strategy_params_json"]
    finally:
        conn.close()

    params_override: str | None = None
    if "initial_budget_quote" in body:
        merged = merge_strategy_params_json(
            existing_params if isinstance(existing_params, str) else None,
            {},
        )
        try:
            merged["initial_budget_quote"] = parse_initial_budget_api_value(
                body["initial_budget_quote"]
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        params_override = json.dumps(merged, default=str)

    fork_name = body.get("name")
    if fork_name is not None and not isinstance(fork_name, str):
        raise HTTPException(status_code=400, detail="name must be a string")
    try:
        out = fork_bot(
            bot_id,
            name=fork_name if isinstance(fork_name, str) else None,
            strategy_params_json=params_override,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail="Bot not found") from None
    return out


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
