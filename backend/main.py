import json
import sys
import os
import subprocess
import time
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
    set_bot_execution_mode,
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
from trading.strategies.registry import (
    get_strategy,
    strategy_names,
    strategy_catalog,
)

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio
    from services.bot_runner import run_async as _bot_runner_async
    from services.data_collector import run_async as _data_collector_async
    init_db()
    # Pause any bots that were left running — the user must explicitly start them.
    conn = get_db_connection()
    try:
        conn.execute("UPDATE bots SET status = 'paused' WHERE status = 'running'")
        conn.commit()
    finally:
        conn.close()
    bot_task = asyncio.create_task(_bot_runner_async())
    collector_task = asyncio.create_task(_data_collector_async())
    try:
        yield
    finally:
        for t in (bot_task, collector_task):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass


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

# Data collector runs as an asyncio.Task inside lifespan — always active.
_DATA_COLLECTOR_MANAGED = True


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
def get_data_status():
    """Data collector is always active as a managed asyncio task."""
    return {"active": _DATA_COLLECTOR_MANAGED, "managed": True}


@app.get("/api/db/stats")
def get_db_stats():
    """Return real DB file size and per-table row counts for the Data page."""
    db_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")
    )
    file_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0

    tables = [
        "market_ticks",
        "bot_orders",
        "bot_logs",
        "bot_decisions",
        "market_depth",
        "bots",
    ]
    table_counts: dict[str, int] = {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for tbl in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")  # noqa: S608 – table names are hard-coded
                table_counts[tbl] = cur.fetchone()[0]
            except Exception:
                table_counts[tbl] = 0
    finally:
        conn.close()

    total = max(sum(table_counts.values()), 1)
    distribution = [
        {
            "table": tbl,
            "rows": table_counts[tbl],
            "pct": round(table_counts[tbl] / total * 100, 1),
        }
        for tbl in tables
        if table_counts[tbl] > 0
    ]
    distribution.sort(key=lambda x: x["rows"], reverse=True)

    return {
        "file_size_bytes": file_size_bytes,
        "file_size_mb": round(file_size_bytes / 1_048_576, 1),
        "table_counts": table_counts,
        "total_ticks": table_counts.get("market_ticks", 0),
        "total_orders": table_counts.get("bot_orders", 0),
        "distribution": distribution,
    }


@app.post("/api/data/purge-sim-logs")
def purge_sim_logs():
    """Delete bot_logs and bot_orders rows for all testnet bots."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT bot_id FROM bots WHERE execution_mode != 'live'"
        )
        testnet_ids = [r["bot_id"] for r in cur.fetchall()]
        deleted_logs = deleted_orders = 0
        for bid in testnet_ids:
            cur.execute("DELETE FROM bot_logs WHERE bot_id = ?", (bid,))
            deleted_logs += cur.rowcount
            cur.execute("DELETE FROM bot_orders WHERE bot_id = ?", (bid,))
            deleted_orders += cur.rowcount
        conn.commit()
        return {
            "deleted_logs": deleted_logs,
            "deleted_orders": deleted_orders,
            "bots_affected": len(testnet_ids),
        }
    finally:
        conn.close()


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
            # Compute lightweight P&L from stored orders (no exchange call)
            bot_id = row.get("bot_id", "")
            sym = str(row.get("symbol") or "")
            try:
                orders_asc = fetch_bot_orders_chronological(bot_id)
                perf = compute_strategy_performance(orders_asc, sym)
                row["realized_pnl_quote"] = round(perf["realized_pnl_quote"], 4)
                row["win_rate_pct"] = perf["win_rate_pct"]
                row["closed_trades"] = perf["closed_trades"]
            except Exception:
                row["realized_pnl_quote"] = None
                row["win_rate_pct"] = None
                row["closed_trades"] = None
        return {"bots": rows}
    finally:
        conn.close()


class CreateBotBody(BaseModel):
    name: str
    symbol: str
    strategy: str = "sma_cross"  # defaults to original strategy for backward compat
    initial_budget_quote: float
    strategy_params: dict[str, Any] | None = None


@app.get("/api/strategies")
def list_strategies():
    """Return all available strategy names, display names, and their default params."""
    return {"strategies": strategy_catalog()}


@app.post("/api/bots", status_code=201)
def post_create_bot(body: CreateBotBody):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name must not be empty")
    if not body.symbol.strip():
        raise HTTPException(status_code=400, detail="symbol must not be empty")
    if body.initial_budget_quote <= 0:
        raise HTTPException(status_code=400, detail="initial_budget_quote must be positive")
    try:
        strategy_mod = get_strategy(body.strategy)
    except ValueError:
        available = strategy_names()
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy {body.strategy!r}. Available: {available}",
        )
    params = strategy_mod.default_params()
    if body.strategy_params:
        # Accept all keys declared by the strategy's default_params, plus universal trading keys.
        universal_keys = {"quote_fraction", "base_fraction", "min_trade_interval_sec", "ohlcv_timeframe", "ohlcv_limit"}
        safe_keys = set(params.keys()) | universal_keys
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


class BotExecutionModeBody(BaseModel):
    execution_mode: str  # "testnet" | "live"


@app.put("/api/bots/{bot_id}/execution-mode")
def put_bot_execution_mode(bot_id: str, body: BotExecutionModeBody):
    """
    Promote a bot from Testnet → Live Spot, or demote back to Testnet.
    The bot must be stopped first. The exact same strategy code is used;
    only the Binance endpoint changes (testnet.binance.vision vs api.binance.com).
    """
    try:
        bot = set_bot_execution_mode(bot_id, body.execution_mode)
    except ValueError as e:
        msg = str(e)
        code = 409 if "stop" in msg or "running" in msg else 404
        raise HTTPException(status_code=code, detail=msg) from e
    return {"bot": bot}


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
        # Use the bot's own execution_mode, not the global setting
        bot_mode = str(bot_row.get("execution_mode") or "testnet")
        sync_bot_orders_from_logs(bot_id, sym)
        try:
            refresh_stale_bot_orders_from_exchange(bot_id, build_binance_spot(bot_mode))
        except Exception:
            pass
        order_stats, orders = fetch_bot_orders_panel(bot_id, order_limit=50)
        orders_asc = fetch_bot_orders_chronological(bot_id)
        mark_price: float | None = None
        if sym:
            try:
                ex = build_binance_spot(bot_mode)
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

        # Portfolio distribution: how much is in the base asset vs quote currency.
        # quote_remaining is computed from cost-basis accounting (independent of
        # mark-price), so it stays accurate even if the bot overspent its budget
        # due to a bug in an older runner version.
        #   quote_remaining = budget + realized_pnl - open_cost_basis
        # This equals: money we started with, plus profits booked, minus what is
        # currently locked in the open position.
        open_base = perf["open_base_position"]
        open_basis = perf["open_cost_basis_quote"]
        realized_pnl = perf["realized_pnl_quote"]

        base_value_quote: float | None = None
        if open_base > 1e-12:
            if mark_price is not None and mark_price > 0:
                base_value_quote = round(open_base * mark_price, 8)
            else:
                base_value_quote = round(open_basis, 8)

        quote_remaining: float | None = None
        base_alloc_pct: float | None = None
        quote_alloc_pct: float | None = None

        if budget is not None and budget > 0:
            # Actual USDT still available = budget + closed-trade profits - cost of open lots
            qr = budget + realized_pnl - open_basis
            quote_remaining = round(max(0.0, qr), 8)
            bv = base_value_quote or 0.0
            # Portfolio denominator: current value of open position + free quote
            portfolio_total = bv + quote_remaining
            if portfolio_total > 1e-12:
                base_alloc_pct = round((bv / portfolio_total) * 100, 2)
                quote_alloc_pct = round(100.0 - base_alloc_pct, 2)
            else:
                base_alloc_pct = 0.0
                quote_alloc_pct = 100.0

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
            "base_value_quote": base_value_quote,
            "quote_remaining": quote_remaining,
            "base_alloc_pct": base_alloc_pct,
            "quote_alloc_pct": quote_alloc_pct,
        }
        return {
            "bot": bot_row,
            "logs": logs,
            "execution_mode": bot_mode,
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
        if body.status == "running":
            cur.execute(
                "UPDATE bots SET status = ?, started_at = ? WHERE bot_id = ?",
                (body.status, int(time.time()), bot_id),
            )
        elif body.status == "stopped":
            # Clear started_at when fully stopped
            cur.execute(
                "UPDATE bots SET status = ?, started_at = NULL WHERE bot_id = ?",
                (body.status, bot_id),
            )
        else:
            cur.execute(
                "UPDATE bots SET status = ? WHERE bot_id = ?",
                (body.status, bot_id),
            )
        conn.commit()
    finally:
        conn.close()

    return {
        "bot_id": bot_id,
        "status": body.status,
    }
