"""
Polls DB for bots with status=running and executes the configured strategy on Binance
(Testnet by default, mainnet only when execution_mode=live in app_settings).

Runs as an asyncio.Task inside uvicorn's lifespan (main.py). Do NOT start as a
subprocess — it will create a duplicate runner. The __main__ block is retained only
for occasional standalone debugging.
"""
from __future__ import annotations

import concurrent.futures
import json
import math
import os
import sys
import threading
import time
import traceback
from collections import defaultdict
from typing import Any

# region path / env
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Force UTF-8 output so Unicode characters never crash on Windows cp1252 consoles.
# Wrapped in try/except because stdout may already be a broken pipe when spawned
# as a subprocess by uvicorn (reconfigure raises on closed handles).
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from dotenv import load_dotenv

load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)
# endregion

from database import (
    get_db_connection,
    record_bot_order,
    record_bot_decision,
    batch_insert_bot_logs,
    batch_insert_voter_feedback,
    batch_record_bot_decisions,
    fetch_bot_orders_chronological,
    insert_voter_feedback,
    upsert_ohlcv_candles,
)
from trading.app_settings import get_execution_mode, is_global_halt
from trading.bot_performance import compute_strategy_performance
from trading.exchange_factory import build_binance_spot, build_binance_public
from trading.strategies.registry import get_strategy, default_params_for
from services.websocket_manager import publish_bot_event, ws_manager
from services.monitoring import monitor

_last_trade_monotonic: dict[str, float] = {}

POLL_SEC = 5


def _slow_cycle_breakdown_threshold_ms() -> float:
    try:
        return float(os.environ.get("SLOW_CYCLE_BREAKDOWN_MS", "1500"))
    except ValueError:
        return 1500.0


_enhanced_diag_lock = threading.Lock()
_enhanced_diag_logged = False


def log_enhanced_diagnostics_banner() -> None:
    """Emit once per process startup (embedded runner + standalone __main__)."""
    global _enhanced_diag_logged
    with _enhanced_diag_lock:
        if _enhanced_diag_logged:
            return
        _enhanced_diag_logged = True
    print(
        "[bot_runner] Enhanced Memory + Cycle Breakdown Logging ENABLED",
        flush=True,
    )


def _log_slow_cycle_breakdown(bot_id: str, phase_ms: dict[str, float], total_ms: float) -> None:
    """Structured multi-line timing split for diagnosing slow cycles."""
    f = phase_ms.get("fetch_ms") or 0.0
    s = phase_ms.get("strategy_ms") or 0.0
    d = phase_ms.get("decision_ms") or 0.0
    t = phase_ms.get("trade_ms") or 0.0
    b = phase_ms.get("broadcast_ms") or 0.0
    summed = f + s + d + t + b
    print(
        f"[SLOW CYCLE BREAKDOWN] bot={bot_id}\n"
        f"  Fetch candles: {f:6.0f}ms\n"
        f"  Strategy calc: {s:6.0f}ms\n"
        f"  Decision:      {d:6.0f}ms\n"
        f"  Trade exec:    {t:6.0f}ms\n"
        f"  Broadcast:     {b:6.0f}ms\n"
        f"  Total (wall):  {total_ms:6.0f}ms"
        + (
            f"\n  (phase sum:     {summed:6.0f}ms differs from wall — thread/setup overhead)"
            if abs(summed - total_ms) > 150
            else ""
        ),
        flush=True,
    )


_last_throttled_bot_info: dict[str, float] = {}
_last_throttled_print: dict[str, float] = {}

# Per-cycle log queue — all bot_log rows written during one _run_one_cycle()
# call are collected here and flushed in a single batch transaction at the end.
# Protected by a lock because multiple bot threads append concurrently.
_log_queue: list[tuple] = []
_log_queue_lock = threading.Lock()
_ws_log_seq = 0
_ws_log_seq_lock = threading.Lock()

# ---------------------------------------------------------------------------
# WS event throttling configuration
# ---------------------------------------------------------------------------

# How often (seconds) to re-broadcast bot_cooldown per bot.  Every 5-second
# cycle would send one event per bot regardless of activity — throttling to
# ~30 s cuts cooldown noise by ~6× with no meaningful UX loss.
_WS_COOLDOWN_INTERVAL_SEC: float = float(
    os.environ.get("WS_COOLDOWN_INTERVAL_SEC", "30")
)

# How often (seconds) to re-broadcast bot_cycle_complete per bot.
# This event is informational noise in steady state; once per minute is enough.
_WS_CYCLE_COMPLETE_INTERVAL_SEC: float = float(
    os.environ.get("WS_CYCLE_COMPLETE_INTERVAL_SEC", "60")
)

# Per-bot timestamps for WS-level throttling (monotonic clock, not wall time).
_last_cooldown_ws: dict[str, float] = {}
_last_cycle_complete_ws: dict[str, float] = {}

# Per-bot WS log buffer — log entries are accumulated during a cycle and
# flushed as a single bot_log_batch event at the end of _run_bot().
# This replaces the old one-event-per-log-line approach that could generate
# 5–8 bot_log events per bot per cycle (30–50 total with 6 bots).
# Protected by a lock because multiple bot threads write concurrently.
_ws_log_buffers: dict[str, list] = defaultdict(list)
_ws_log_buffer_lock = threading.Lock()


def _next_ws_log_id(created_at: int) -> int:
    global _ws_log_seq
    with _ws_log_seq_lock:
        _ws_log_seq = (_ws_log_seq + 1) % 100_000
        return -((created_at * 100_000) + _ws_log_seq)


def _emit_bot_event(
    bot_id: str,
    event_type: str,
    data: dict[str, Any],
    *,
    priority: bool = False,
) -> None:
    try:
        publish_bot_event(bot_id, event_type, data, priority=priority)
    except Exception:
        pass


def _format_meta(meta: dict) -> str:
    """Render strategy meta-dict as a compact key=value string for log messages."""
    parts = []
    for k, v in meta.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:.4f}")
        elif v is None:
            pass
        else:
            parts.append(f"{k}={v}")
    return "  ".join(parts) if parts else "(no meta)"


def _get_features_snapshot(symbol: str) -> str | None:
    """
    Fetch the latest ``features_json`` blob from ``market_ticks`` for
    ``symbol``.  Returns a JSON string ready for storage, or ``None`` if
    no recent tick is available.

    Wrapped in a broad try/except — a missing snapshot must never interrupt
    trade execution or voter feedback logging.
    """
    try:
        t0 = time.perf_counter()
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT features_json FROM market_ticks
                WHERE target_asset = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (symbol,),
            )
            row = cur.fetchone()
        finally:
            conn.close()
        monitor.record_db_op(f"get_features_snapshot({symbol})", (time.perf_counter() - t0) * 1000)
        if row and row["features_json"]:
            return row["features_json"]
    except Exception:
        pass
    return None


def _log_voter_feedback(bot_id: str, symbol: str, result: Any) -> None:
    """
    Persist per-voter votes to voter_feedback for MetaMagi training.

    Called only for ensemble strategies (detected via 'voter_signals' in
    result.meta).  All voters for this bot+cycle are collected into a single
    list and written in one batch transaction, replacing the old per-voter
    INSERT loop that caused 80–120 separate transactions per 5-second cycle.

    forward_roc_* / realized_pnl are filled later by meta_training_loop.
    Failures are silently swallowed — must never interrupt trade execution.
    """
    meta = result.meta if isinstance(result.meta, dict) else {}
    voter_signals: dict = meta.get("voter_signals") or {}
    voter_confidences: dict = meta.get("voter_confidences") or {}
    if not voter_signals:
        return

    ts = int(time.time() * 1000)
    consensus_score = meta.get("consensus_score")
    # Capture the current market microstructure snapshot once per cycle so
    # all voters in this ensemble share the same feature context.  This is
    # the primary feature vector MetaMagi uses for future neural-net training.
    features_snapshot = _get_features_snapshot(symbol)

    records = [
        {
            "bot_id": bot_id,
            "timestamp": ts,
            "target_asset": symbol,
            "ensemble_signal": result.signal,
            "voter_name": voter_name,
            "voter_signal": voter_signal,
            "confidence": voter_confidences.get(voter_name),
            "consensus_score": consensus_score,
            "features_snapshot": features_snapshot,
        }
        for voter_name, voter_signal in voter_signals.items()
    ]
    try:
        batch_insert_voter_feedback(records)
        _emit_bot_event(
            bot_id,
            "voter_signals",
            {
                "symbol": symbol,
                "voter_signals": [
                    {
                        "voter_name": r["voter_name"],
                        "voter_signal": r["voter_signal"],
                        "confidence": r["confidence"],
                        "consensus_score": r["consensus_score"],
                        "timestamp": r["timestamp"],
                    }
                    for r in records
                ],
            },
        )
    except Exception:
        pass


def _idle_log_interval_sec() -> float:
    try:
        return float(os.environ.get("BOT_IDLE_LOG_INTERVAL_SEC", "45"))
    except ValueError:
        return 45.0


# Set BOT_DEBUG_LOGS=0 (or false/no) to hide extra numeric detail lines in bot_logs.
def _debug_logs_enabled() -> bool:
    raw = (os.environ.get("BOT_DEBUG_LOGS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _log(bot_id: str, level: str, execution_mode: str, message: str) -> None:
    # Print with HH:MM:SS timestamp so stalls in the terminal are immediately visible.
    ts = time.strftime("%H:%M:%S")
    created_at = int(time.time() * 1000)
    try:
        print(f"{ts} [{level}] [{execution_mode}] bot={bot_id} {message}", flush=True)
    except (OSError, ValueError):
        # Stdout pipe is broken or closed (common when spawned as a subprocess).
        pass
    # Queue the DB row for bulk insert at end of cycle (_flush_log_queue).
    # This replaces the old per-line open/commit/close that caused lock contention.
    with _log_queue_lock:
        _log_queue.append(
            (bot_id, created_at, level, execution_mode, message)
        )
    # Buffer the WS log entry — flushed as a single bot_log_batch event at the
    # end of _run_bot() instead of firing one WebSocket message per log line.
    # With 6 bots emitting 5–8 logs each this cuts WS traffic by ~30×.
    with _ws_log_buffer_lock:
        _ws_log_buffers[bot_id].append({
            "log_id": _next_ws_log_id(created_at),
            "bot_id": bot_id,
            "created_at": created_at,
            "level": level,
            "execution_mode": execution_mode,
            "message": message,
        })


def _flush_ws_log_buffer(bot_id: str) -> None:
    """Emit all buffered log entries for this bot as a single WS batch event.

    Called once at the end of each _run_bot() invocation so the entire cycle's
    logs cost exactly one WebSocket message instead of one per log line.
    """
    with _ws_log_buffer_lock:
        buf = _ws_log_buffers.get(bot_id)
        if not buf:
            return
        logs = list(buf)
        buf.clear()
    if logs:
        _emit_bot_event(bot_id, "bot_log_batch", {"logs": logs})


def _flush_log_queue() -> None:
    """
    Write all queued bot_log rows to the DB in a single transaction.

    Called once at the end of every _run_one_cycle() — after all bot threads
    have completed — so the entire cycle's logs cost exactly one DB commit
    instead of one commit per log line (was ~30–50 commits per 5-second cycle).
    """
    with _log_queue_lock:
        if not _log_queue:
            return
        rows = list(_log_queue)
        _log_queue.clear()
    try:
        batch_insert_bot_logs(rows)
    except Exception:
        pass


def _log_info_throttled(
    bot_id: str,
    execution_mode: str,
    throttle_key: str,
    message: str,
) -> None:
    now = time.monotonic()
    k = f"{bot_id}:{throttle_key}"
    interval = _idle_log_interval_sec()
    if now - _last_throttled_bot_info.get(k, 0.0) < interval:
        return
    _last_throttled_bot_info[k] = now
    _log(bot_id, "info", execution_mode, message)


def _throttled_print(key: str, message: str) -> None:
    now = time.monotonic()
    interval = _idle_log_interval_sec()
    if now - _last_throttled_print.get(key, 0.0) < interval:
        return
    _last_throttled_print[key] = now
    print(message)


def _load_running_bots():
    t0 = time.perf_counter()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM bots WHERE status = 'running' ORDER BY bot_id"
        )
        result = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
    monitor.record_db_op("load_running_bots", (time.perf_counter() - t0) * 1000)
    return result


def _merge_params(strategy_name: str, raw: str | None) -> dict[str, Any]:
    base = default_params_for(strategy_name)
    if not raw:
        return base
    try:
        return {**base, **json.loads(raw)}
    except json.JSONDecodeError:
        return base


def _f_order(x: Any) -> float:
    """Safe float coerce for order fields."""
    try:
        v = float(x)
        return v if v == v else 0.0  # guard NaN
    except (TypeError, ValueError):
        return 0.0


def _log_post_trade_balance(
    bot_id: str,
    execution_mode: str,
    ex: Any,
    base_cur: str,
    quote_cur: str,
) -> None:
    """Fetch and log wallet balances immediately after a fill."""
    try:
        bal = ex.fetch_balance()
        free_base = float(bal.get("free", {}).get(base_cur, 0) or 0)
        free_quote = float(bal.get("free", {}).get(quote_cur, 0) or 0)
        _log(
            bot_id,
            "info",
            execution_mode,
            f"Wallet after trade: {base_cur}={free_base:.8f}  {quote_cur}={free_quote:.4f}",
        )
        _emit_bot_event(
            bot_id,
            "wallet_update",
            {
                "execution_mode": execution_mode,
                "balances": {
                    base_cur: {"free": free_base},
                    quote_cur: {"free": free_quote},
                },
            },
            priority=True,
        )
    except Exception:
        pass


def _get_bot_position(bot_id: str, symbol: str) -> tuple[float, float]:
    """
    Returns (open_base_position, open_cost_basis_quote) for this bot
    by replaying its order history using FIFO accounting.
    Used to constrain trades to the bot's configured budget.
    """
    try:
        orders = fetch_bot_orders_chronological(bot_id)
        perf = compute_strategy_performance(orders, symbol)
        return float(perf["open_base_position"]), float(perf["open_cost_basis_quote"])
    except Exception:
        return 0.0, 0.0


def _process_bot(
    ex,
    bot: dict[str, Any],
    execution_mode: str,
    prefetched_ohlcv: list | None = None,
    public_ex=None,
    phase_ms: dict[str, float] | None = None,
) -> None:
    """
    ex         – authenticated exchange (testnet/live) for balance + orders.
    public_ex  – unauthenticated mainnet exchange for OHLCV + market metadata.
                 Falls back to ``ex`` when not provided (legacy path).
    """
    timings = phase_ms if phase_ms is not None else {}
    for _k in ("fetch_ms", "strategy_ms", "decision_ms", "trade_ms"):
        timings.setdefault(_k, 0.0)

    bot_id = bot["bot_id"]
    symbol = bot["symbol"]
    strategy_name = bot.get("strategy") or "sma_cross"
    params = _merge_params(strategy_name, bot.get("strategy_params_json"))
    data_ex = public_ex if public_ex is not None else ex  # real prices for signals

    # Collect bot_decisions for this cycle into a list; flush once at the end
    # (or before any early return that follows a decision).  Each bot runs in
    # its own thread — this list is local, so no locking is required.
    _pending_decisions: list[dict] = []

    def _queue_decision(action: str, confidence: float | None, executed: bool) -> None:
        _pending_decisions.append({
            "bot_id": bot_id,
            "symbol": symbol,
            "mode": execution_mode,
            "action": action,
            "confidence": confidence,
            "executed": executed,
        })

    def _flush_decisions() -> None:
        if _pending_decisions:
            try:
                batch_record_bot_decisions(_pending_decisions)
            except Exception:
                pass

    interval_key = float(params["min_trade_interval_sec"])
    last = _last_trade_monotonic.get(bot_id, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < interval_key:
        left = max(0.0, interval_key - elapsed)
        # Throttle bot_cooldown broadcasts: sending one per cycle (every 5 s) per
        # bot generates pure noise — cap to once per _WS_COOLDOWN_INTERVAL_SEC.
        now_mono = time.monotonic()
        if now_mono - _last_cooldown_ws.get(bot_id, 0.0) >= _WS_COOLDOWN_INTERVAL_SEC:
            _last_cooldown_ws[bot_id] = now_mono
            _emit_bot_event(
                bot_id,
                "bot_cooldown",
                {
                    "symbol": symbol,
                    "execution_mode": execution_mode,
                    "remaining_sec": round(left, 3),
                    "interval_sec": interval_key,
                },
            )
        _log_info_throttled(
            bot_id,
            execution_mode,
            "cooldown",
            f"Cooldown: {left:.0f}s remaining before next market check "
            f"(interval={interval_key:.0f}s).",
        )
        return

    timeframe = str(params.get("ohlcv_timeframe", "5m"))
    limit = int(params.get("ohlcv_limit", 50))

    t_fetch0 = time.perf_counter()
    if prefetched_ohlcv is not None:
        # Shared candle data fetched once for all bots on this symbol/timeframe.
        ohlcv = prefetched_ohlcv
        _log(bot_id, "info", execution_mode,
             f"Cycle: {symbol} {timeframe} — {len(ohlcv)} candles (shared fetch, mainnet)")
    else:
        _log(bot_id, "info", execution_mode,
             f"Cycle: {symbol} {timeframe} — fetching {limit} candles from mainnet…")
        try:
            ohlcv = data_ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            _log(bot_id, "error", execution_mode, f"fetch_ohlcv failed: {e}")
            timings["fetch_ms"] = (time.perf_counter() - t_fetch0) * 1000.0
            return

    timings["fetch_ms"] = (time.perf_counter() - t_fetch0) * 1000.0

    if not ohlcv:
        _log(bot_id, "warn", execution_mode,
             f"OHLCV empty for {symbol} {timeframe} — retrying next poll.")
        return

    last_close = float(ohlcv[-1][4])
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Market: last_close={last_close:.4f}  candles={len(ohlcv)}",
    )

    t_strat0 = time.perf_counter()
    try:
        strategy = get_strategy(strategy_name)
    except ValueError as e:
        timings["strategy_ms"] = (time.perf_counter() - t_strat0) * 1000.0
        _log(bot_id, "error", execution_mode, f"Unknown strategy {strategy_name!r}: {e}")
        return

    result = strategy.evaluate(ohlcv, params)
    _emit_bot_event(
        bot_id,
        "bot_signal",
        {
            "symbol": symbol,
            "execution_mode": execution_mode,
            "signal": result.signal,
            "confidence": result.confidence,
            "warmup": result.warmup,
            "close_count": result.close_count,
            "meta": result.meta if isinstance(result.meta, dict) else {},
        },
    )
    timings["strategy_ms"] = (time.perf_counter() - t_strat0) * 1000.0

    t_dec0 = time.perf_counter()
    decision_closed = False

    def _close_decision() -> None:
        nonlocal decision_closed
        if decision_closed:
            return
        timings["decision_ms"] = (time.perf_counter() - t_dec0) * 1000.0
        decision_closed = True

    # Log per-voter votes for MetaMagi feedback on every tick (ensemble only).
    # Must happen before any early return so HOLD ticks are also recorded.
    _log_voter_feedback(bot_id, symbol, result)

    if result.signal == "hold":
        if result.warmup:
            _log(
                bot_id,
                "info",
                execution_mode,
                f"Signal: HOLD (warmup) — {result.close_count} bars collected.",
            )
        else:
            meta_str = _format_meta(result.meta)
            _log(bot_id, "info", execution_mode, f"Signal: HOLD — {meta_str}")
        _queue_decision("HOLD", None, False)
        _flush_decisions()
        _close_decision()
        return

    meta_str = _format_meta(result.meta)
    _log(bot_id, "info", execution_mode, f"Signal: {result.signal.upper()} — {meta_str}")

    _signal_confidence: float | None = result.confidence

    try:
        if not data_ex.markets:
            data_ex.load_markets()
        market = data_ex.market(symbol)
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"market metadata failed: {e}")
        _close_decision()
        return

    limits = market.get("limits") or {}
    min_amt = float((limits.get("amount") or {}).get("min") or 0)
    min_cost = float((limits.get("cost") or {}).get("min") or 0)

    try:
        balance = ex.fetch_balance()
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"fetch_balance failed: {e}")
        _close_decision()
        return

    base_cur, quote_cur = market["base"], market["quote"]
    free_base = float(balance.get("free", {}).get(base_cur, 0) or 0)
    free_quote = float(balance.get("free", {}).get(quote_cur, 0) or 0)
    _log(
        bot_id,
        "info",
        execution_mode,
        f"Wallet before trade: {base_cur}={free_base:.8f}  {quote_cur}={free_quote:.4f}",
    )

    if result.signal == "buy":
        quote_fraction = float(params["quote_fraction"])
        initial_budget = float(params.get("initial_budget_quote") or 0)

        if initial_budget > 0:
            # Budget-constrained: only trade within the configured budget
            _, open_cost = _get_bot_position(bot_id, symbol)
            remaining_budget = max(0.0, initial_budget - open_cost)
            if remaining_budget < min_cost:
                _log(bot_id, "warn", execution_mode,
                     f"BUY skipped — budget exhausted "
                     f"({remaining_budget:.4f} {quote_cur} remaining < exchange min {min_cost} {quote_cur})")
                _close_decision()
                return
            # Ideal spend = fraction of remaining budget, capped by available funds.
            # Use 2% buffer on the minimum so price slippage between snapshot and fill
            # can't push the executed notional below the exchange minimum.
            min_spend = min_cost * 1.02 if min_cost > 0 else 0.0
            spend = min(quote_fraction * remaining_budget, remaining_budget, free_quote)
            if spend < min_spend:
                spend = min_spend
            _log(bot_id, "info", execution_mode,
                 f"Budget: {remaining_budget:.4f} {quote_cur} remaining of {initial_budget:.2f} {quote_cur} budget")
        else:
            # No budget configured — use exchange wallet as fallback
            min_spend = min_cost * 1.02 if min_cost > 0 else 0.0
            spend = free_quote * quote_fraction
            if spend < min_spend:
                _log(bot_id, "warn", execution_mode,
                     f"BUY skipped — spend {spend:.4f} {quote_cur} below exchange minimum {min_cost} {quote_cur}")
                _close_decision()
                return
        if last_close <= 0:
            _log(bot_id, "warn", execution_mode, "BUY skipped — last_close is zero")
            _close_decision()
            return

        # Compute the buy quantity from spend, then apply lot-size precision.
        # We use create_order(qty) instead of create_market_buy_order_with_cost(cost)
        # because Binance converts cost→qty internally and rounds DOWN, which can leave
        # the actual fill below min_cost — permanently creating an unsellable position.
        qty_raw = spend / last_close
        qty_prec = float(ex.amount_to_precision(symbol, qty_raw))

        # If lot-size rounding dropped the notional below the buffered minimum,
        # ceiling-round the quantity up to the next valid lot step.
        _min_notional = (min_cost * 1.02) if min_cost > 0 else 0.0
        if _min_notional > 0 and qty_prec * last_close < _min_notional:
            try:
                prec_val = market.get("precision", {}).get("amount")
                if prec_val is not None:
                    pv = float(prec_val)
                    # CCXT precision can be in two modes:
                    #   TICK_SIZE    — pv IS the step size  (e.g. 0.00001)
                    #   DECIMAL_PLACES — pv is decimal count (e.g. 5 → step 0.00001)
                    if pv > 0:
                        step = pv if pv < 1 else 10.0 ** (-int(pv))
                        p = max(0, round(-math.log10(step)))
                        min_qty = _min_notional / last_close
                        qty_prec = round(math.ceil(round(min_qty / step, 9)) * step, p)
            except Exception:
                pass  # best-effort; proceed with rounded-down qty

        if qty_prec <= 0:
            _log(bot_id, "warn", execution_mode, "BUY skipped — quantity rounds to zero")
            _close_decision()
            return

        actual_spend = qty_prec * last_close
        budget_ref = remaining_budget if initial_budget > 0 else free_quote
        _log(bot_id, "info", execution_mode,
             f"BUY order: spending ~{actual_spend:.4f} {quote_cur} "
             f"({quote_fraction*100:.1f}% of {budget_ref:.4f} {quote_cur} remaining) @ ~{last_close:.2f}")
        _close_decision()
        t_trade0 = time.perf_counter()
        try:
            order = ex.create_order(symbol, "market", "buy", qty_prec)
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost")) or actual_spend
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            _log(bot_id, "info", execution_mode,
                 f"[OK] BUY filled — spent {cost_quote:.4f} {quote_cur} "
                 f"-> received {filled_base:.8f} {base_cur} "
                 f"@ avg {avg_price:.2f} {quote_cur}/{base_cur}  [id={order.get('id')}]")
            _emit_bot_event(
                bot_id,
                "trade_executed",
                {
                    "execution_mode": execution_mode,
                    "symbol": symbol,
                    "side": "buy",
                    "order": {
                        "id": order.get("id"),
                        "amount": order.get("amount"),
                        "cost": cost_quote,
                        "filled": filled_base,
                        "average": avg_price,
                        "status": order.get("status"),
                    },
                },
                priority=True,
            )
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            _queue_decision("BUY", _signal_confidence, True)
        except Exception as e:
            _queue_decision("BUY", _signal_confidence, False)
            _emit_bot_event(
                bot_id,
                "trade_rejected",
                {
                    "execution_mode": execution_mode,
                    "symbol": symbol,
                    "side": "buy",
                    "reason": str(e),
                },
                priority=True,
            )
            _log(bot_id, "error", execution_mode, f"BUY failed: {e}\n{traceback.format_exc()}")
        finally:
            timings["trade_ms"] = (time.perf_counter() - t_trade0) * 1000.0

    elif result.signal == "sell":
        base_fraction = float(params["base_fraction"])
        initial_budget = float(params.get("initial_budget_quote") or 0)

        if initial_budget > 0:
            # Sell only the base position this bot acquired with its own budget
            open_base, _ = _get_bot_position(bot_id, symbol)
            if open_base <= 1e-12:
                _log(bot_id, "info", execution_mode,
                     "SELL skipped — no open position in this bot's budget")
                _close_decision()
                return
            # Cap by what the exchange actually holds (safety net)
            sell_amt = min(open_base * base_fraction, free_base)
        else:
            # No budget configured — use exchange wallet as fallback
            sell_amt = free_base * base_fraction

        # Minimum base-amount check
        if min_amt and sell_amt < min_amt:
            _log(bot_id, "warn", execution_mode,
                 f"SELL skipped — amount {sell_amt:.8f} {base_cur} below exchange minimum {min_amt}")
            _close_decision()
            return

        # Minimum notional check — mirrors the BUY side: bump up to the minimum
        # rather than failing. Only skip if the entire available position is too
        # small (truly stuck; shouldn't happen with a correctly sized budget).
        if min_cost > 0 and last_close > 0:
            notional = sell_amt * last_close
            if notional < min_cost:
                # Bump sell_amt up to exactly the minimum notional, capped by position.
                min_sell_amt = min_cost / last_close
                available = open_base if initial_budget > 0 else free_base
                if min_sell_amt <= available:
                    _log(bot_id, "info", execution_mode,
                         f"SELL: fractional notional {notional:.4f} {quote_cur} < min {min_cost}"
                         f" — bumping to minimum ({min_sell_amt:.8f} {base_cur})")
                    sell_amt = min_sell_amt
                else:
                    # Entire position is below minimum — nothing we can do.
                    full_notional = available * last_close
                    _log(bot_id, "warn", execution_mode,
                         f"SELL skipped — full position notional {full_notional:.4f} {quote_cur}"
                         f" below exchange minimum {min_cost} {quote_cur}")
                    _close_decision()
                    return

        sell_prec = ex.amount_to_precision(symbol, sell_amt)
        if float(sell_prec) <= 0:
            _log(bot_id, "warn", execution_mode,
                 f"SELL skipped — rounded amount is zero (raw {sell_amt:.8f} {base_cur})")
            _close_decision()
            return

        # Final notional guard after precision rounding.
        # amount_to_precision truncates (rounds down), so the bumped amount can land
        # on a step that is still below min_cost.  Mirror the BUY-side fix: use
        # math.ceil to the nearest lot-size step so the rounded quantity is always
        # >= the minimum notional requirement.
        final_notional = float(sell_prec) * last_close
        if min_cost > 0 and final_notional < min_cost:
            available = (open_base if initial_budget > 0 else free_base)
            ceiled = False
            try:
                prec_val = market.get("precision", {}).get("amount")
                if prec_val is not None:
                    pv = float(prec_val)
                    if pv > 0:
                        # TICK_SIZE mode: pv is the step (e.g. 0.00001)
                        # DECIMAL_PLACES mode: pv is decimal count (e.g. 5 → step 1e-5)
                        step = pv if pv < 1 else 10.0 ** (-int(pv))
                        p = max(0, round(-math.log10(step)))
                        min_qty = min_cost / last_close
                        ceiled_qty = round(math.ceil(round(min_qty / step, 9)) * step, p)
                        if ceiled_qty <= available:
                            sell_prec = ceiled_qty
                            final_notional = float(sell_prec) * last_close
                            _log(bot_id, "info", execution_mode,
                                 f"SELL: post-rounding notional {final_notional:.4f} {quote_cur}"
                                 f" — ceiled to {sell_prec} {base_cur} to clear NOTIONAL filter")
                            ceiled = True
                        else:
                            _log(bot_id, "warn", execution_mode,
                                 f"SELL skipped — post-rounding notional {final_notional:.4f} {quote_cur}"
                                 f" below exchange minimum {min_cost} {quote_cur} and cannot bump further")
                            _close_decision()
                            return
            except Exception:
                pass  # fall through to the old margin approach below
            if not ceiled:
                bumped_amt = (min_cost * 1.02) / last_close
                if bumped_amt <= available:
                    sell_prec = ex.amount_to_precision(symbol, bumped_amt)
                    final_notional = float(sell_prec) * last_close
                    _log(bot_id, "info", execution_mode,
                         f"SELL: post-rounding notional {final_notional:.4f} {quote_cur}"
                         f" — re-bumped to {sell_prec} {base_cur} to clear NOTIONAL filter")
                else:
                    _log(bot_id, "warn", execution_mode,
                         f"SELL skipped — post-rounding notional {final_notional:.4f} {quote_cur}"
                         f" below exchange minimum {min_cost} {quote_cur} and cannot bump further")
                    _close_decision()
                    return

        _log(bot_id, "info", execution_mode,
             f"SELL order: selling {sell_prec} {base_cur} "
             f"(notional ~{final_notional:.4f} {quote_cur}) @ ~{last_close:.2f}")
        _close_decision()
        t_trade0 = time.perf_counter()
        try:
            order = ex.create_order(symbol, "market", "sell", float(sell_prec))
            _last_trade_monotonic[bot_id] = time.monotonic()
            record_bot_order(bot_id, execution_mode, order)
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost"))
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            _log(bot_id, "info", execution_mode,
                 f"[OK] SELL filled — sold {filled_base:.8f} {base_cur} "
                 f"→ received {cost_quote:.4f} {quote_cur} "
                 f"@ avg {avg_price:.2f} {quote_cur}/{base_cur}  [id={order.get('id')}]")
            _emit_bot_event(
                bot_id,
                "trade_executed",
                {
                    "execution_mode": execution_mode,
                    "symbol": symbol,
                    "side": "sell",
                    "order": {
                        "id": order.get("id"),
                        "amount": order.get("amount"),
                        "cost": cost_quote,
                        "filled": filled_base,
                        "average": avg_price,
                        "status": order.get("status"),
                    },
                },
                priority=True,
            )
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            _queue_decision("SELL", _signal_confidence, True)
        except Exception as e:
            _queue_decision("SELL", _signal_confidence, False)
            _emit_bot_event(
                bot_id,
                "trade_rejected",
                {
                    "execution_mode": execution_mode,
                    "symbol": symbol,
                    "side": "sell",
                    "reason": str(e),
                },
                priority=True,
            )
            _log(bot_id, "error", execution_mode, f"SELL failed: {e}\n{traceback.format_exc()}")
        finally:
            timings["trade_ms"] = (time.perf_counter() - t_trade0) * 1000.0

    else:
        # Strategies should only emit hold / buy / sell; close the slice anyway.
        _close_decision()

    # Flush any queued decision for this cycle (BUY or SELL branch).
    # HOLD already flushed before its early return; early exits before order
    # placement have no queued decision, so this is a no-op for them.
    _flush_decisions()


def _build_exchanges_for_bots(bots: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build one CCXT exchange instance per unique execution_mode in the bot list.
    Each bot carries its own execution_mode ('testnet' | 'live'), so a single
    runner can serve mixed-mode bots safely.
    """
    exchanges: dict[str, Any] = {}
    modes_needed = {bot.get("execution_mode", "testnet") for bot in bots}
    for mode in modes_needed:
        if mode in exchanges:
            continue
        try:
            ex = build_binance_spot(mode)
            # ccxt timeout is in milliseconds.  15 s is generous for testnet
            # but still short enough that a dead connection unblocks quickly.
            ex.timeout = 15_000
            exchanges[mode] = ex
        except ValueError as e:
            print(f"[bot_runner] Exchange config error for mode={mode}: {e}")
    return exchanges


async def run_async():
    """Async entry-point embedded as an asyncio task in uvicorn's lifespan."""
    import asyncio
    loop = asyncio.get_event_loop()
    log_enhanced_diagnostics_banner()
    print("Bot runner started — polling for running bots.")
    while True:
        try:
            # Run the synchronous cycle in a thread-pool executor so ccxt's
            # blocking HTTP calls never freeze the asyncio event loop.
            # A 120 s hard cap ensures a completely stuck cycle doesn't prevent
            # the next one from starting.
            await asyncio.wait_for(
                loop.run_in_executor(None, _run_one_cycle),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            print(
                "[bot_runner] Full cycle exceeded 120 s wall time — "
                "skipping to next poll."
            )
        except Exception:
            pass
        await asyncio.sleep(POLL_SEC)


_public_exchange = None  # lazily initialised mainnet read-only instance


def _get_public_exchange():
    """Return (or create) the shared unauthenticated mainnet exchange for OHLCV."""
    global _public_exchange
    if _public_exchange is None:
        _public_exchange = build_binance_public()
    return _public_exchange


def _run_one_cycle():
    """One polling cycle — extracted so both the async and sync entrypoints share it."""
    _cycle_wall_t0 = time.perf_counter()

    if is_global_halt():
        _throttled_print(
            "global_halt",
            "[bot_runner] Waiting: global trading halt is ON — no bot cycles.",
        )
        return

    bots = _load_running_bots()
    if not bots:
        _throttled_print(
            "no_running_bots",
            f"[bot_runner] Waiting: no bots with status=running in DB (sleep {POLL_SEC}s).",
        )
        return

    # Authenticated exchanges (per execution_mode) — used only for balance + orders.
    exchanges = _build_exchanges_for_bots(bots)
    # Single unauthenticated mainnet exchange — used for OHLCV + market metadata.
    public_ex = _get_public_exchange()

    # ── Pre-fetch OHLCV once per unique (symbol, timeframe, limit) ────────────
    # All bots share the same mainnet candle data regardless of their execution_mode,
    # so signals are always computed from real market prices.
    ohlcv_cache: dict[tuple, list | None] = {}
    for bot in bots:
        params = _merge_params(bot.get("strategy") or "sma_cross", bot.get("strategy_params_json"))
        # Skip pre-fetch for bots still in cooldown — they won't use the data.
        interval_key = float(params["min_trade_interval_sec"])
        if time.monotonic() - _last_trade_monotonic.get(bot["bot_id"], 0.0) < interval_key:
            continue
        symbol = bot["symbol"]
        timeframe = str(params.get("ohlcv_timeframe", "5m"))
        limit = int(params.get("ohlcv_limit", 50))
        cache_key = (symbol, timeframe, limit)  # mode-independent: always mainnet
        if cache_key not in ohlcv_cache:
            try:
                if not public_ex.markets:
                    public_ex.load_markets()
                # Hard timeout on the network call so one slow exchange response
                # can't hold up the entire pre-fetch loop.
                public_ex.timeout = 15_000  # milliseconds (ccxt option)
                candles = public_ex.fetch_ohlcv(
                    symbol, timeframe=timeframe, limit=limit
                )
                ohlcv_cache[cache_key] = candles
                # Persist candles for backtesting replay — failures are
                # swallowed inside upsert_ohlcv_candles, never blocking trades.
                upsert_ohlcv_candles(symbol, timeframe, candles)
            except Exception as e:
                print(f"[bot_runner] pre-fetch OHLCV failed {symbol} {timeframe}: {e}")
                ohlcv_cache[cache_key] = None  # bot will fall back to its own fetch

    # ── Dispatch all bots in parallel threads ─────────────────────────────────
    def _run_bot(bot: dict[str, Any]) -> None:
        bot_id = bot["bot_id"]
        mode = bot.get("execution_mode", "testnet")
        ex = exchanges.get(mode)
        if ex is None:
            _log(bot_id, "error", mode,
                 f"No exchange for execution_mode={mode!r} — check API keys in .env.")
            return
        params = _merge_params(bot.get("strategy") or "sma_cross", bot.get("strategy_params_json"))
        symbol = bot["symbol"]
        timeframe = str(params.get("ohlcv_timeframe", "5m"))
        limit = int(params.get("ohlcv_limit", 50))
        prefetched = ohlcv_cache.get((symbol, timeframe, limit))

        phase_ms: dict[str, float] = {
            "fetch_ms": 0.0,
            "strategy_ms": 0.0,
            "decision_ms": 0.0,
            "trade_ms": 0.0,
            "broadcast_ms": 0.0,
        }

        _cycle_t0 = time.perf_counter()
        try:
            _process_bot(
                ex,
                bot,
                mode,
                prefetched_ohlcv=prefetched,
                public_ex=public_ex,
                phase_ms=phase_ms,
            )
        except Exception:
            _log(bot_id, "error", mode, f"Unhandled: {traceback.format_exc()}")
        finally:
            # Flush all WS log entries collected during this bot's cycle as a
            # single batch event.  Must run even if _process_bot raised so logs
            # from the error path still reach the UI.
            t_bc = time.perf_counter()
            _flush_ws_log_buffer(bot_id)
            phase_ms["broadcast_ms"] = (time.perf_counter() - t_bc) * 1000.0

        cycle_ms = (time.perf_counter() - _cycle_t0) * 1000
        monitor.record_bot_cycle(bot_id, cycle_ms)

        thresh = _slow_cycle_breakdown_threshold_ms()
        if cycle_ms > thresh:
            _log_slow_cycle_breakdown(bot_id, phase_ms, cycle_ms)

        if cycle_ms > 500:
            print(
                f"[bot_runner] SLOW CYCLE bot={bot_id} symbol={symbol} "
                f"→ {cycle_ms:.0f} ms",
                flush=True,
            )

        # bot_cycle_complete is informational noise at steady state.  Throttle
        # to once per _WS_CYCLE_COMPLETE_INTERVAL_SEC per bot so 6 bots don't
        # fire 6 empty events every 5 seconds.
        now_mono = time.monotonic()
        if now_mono - _last_cycle_complete_ws.get(bot_id, 0.0) >= _WS_CYCLE_COMPLETE_INTERVAL_SEC:
            _last_cycle_complete_ws[bot_id] = now_mono
            _emit_bot_event(
                bot_id,
                "bot_cycle_complete",
                {
                    "symbol": symbol,
                    "execution_mode": mode,
                    "status": bot.get("status"),
                    "cycle_ms": round(cycle_ms, 1),
                },
            )

    # Per-cycle hard deadline.  Each bot gets up to _CYCLE_TIMEOUT_SEC of wall
    # time; after that we move on regardless — the hung thread is ORPHANED, not
    # waited on.
    #
    # Critical design note
    # --------------------
    # We deliberately do NOT use `with ThreadPoolExecutor(...) as pool:`.
    # That context manager calls pool.shutdown(wait=True) on exit, which BLOCKS
    # until every thread finishes — including threads stuck on a hanging Binance
    # TCP connection.  future.cancel() cannot interrupt a running thread, so
    # the old `with` pattern caused the entire runner to freeze for 40+ minutes
    # whenever one bot's network call hung (common on Binance testnet after
    # repeated NOTIONAL filter rejections).
    #
    # pool.shutdown(wait=False) releases the pool immediately.  Orphaned threads
    # clean themselves up when their TCP timeout eventually fires.
    _CYCLE_TIMEOUT_SEC = 90

    max_workers = min(len(bots), 32)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    futures: dict[concurrent.futures.Future, str] = {
        pool.submit(_run_bot, bot): bot["bot_id"] for bot in bots
    }
    try:
        done, not_done = concurrent.futures.wait(
            futures, timeout=_CYCLE_TIMEOUT_SEC
        )
        if not_done:
            bot_ids = ", ".join(futures[f] for f in not_done)
            print(
                f"[bot_runner] Cycle timed out after {_CYCLE_TIMEOUT_SEC}s — "
                f"{len(not_done)} bot(s) still running (network hang?): {bot_ids}"
            )
        for f in done:
            try:
                f.result()
            except Exception:
                pass
    finally:
        # Non-blocking shutdown: don't wait for hung threads.
        pool.shutdown(wait=False)

    # Flush all bot_log rows collected during this cycle in one transaction.
    # Runs after the pool exits — by this point all *finished* threads have
    # appended their rows; orphaned threads may add a few straggler rows to
    # the next cycle's flush, which is acceptable.
    _flush_log_queue()

    # ── Observability ─────────────────────────────────────────────────────────
    wall_ms = (time.perf_counter() - _cycle_wall_t0) * 1000
    if wall_ms > 1000:
        print(
            f"[bot_runner] SLOW full cycle: {len(bots)} bot(s) → {wall_ms:.0f} ms",
            flush=True,
        )

    monitor.maybe_log_health(
        running_bots=len(bots),
        ws_clients=ws_manager.connected_count(),
    )
    monitor.maybe_log_memory(running_bots=len(bots))
    monitor.maybe_log_cycle_trend()


def main():
    """Synchronous blocking loop — used only when running as __main__ (standalone/legacy)."""
    log_enhanced_diagnostics_banner()
    print("Bot runner started — polling for running bots.")
    while True:
        try:
            _run_one_cycle()
            time.sleep(POLL_SEC)
        except KeyboardInterrupt:
            print("Bot runner stopped.")
            break
        except Exception:
            try:
                print(traceback.format_exc())
            except (OSError, ValueError):
                pass
            time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
