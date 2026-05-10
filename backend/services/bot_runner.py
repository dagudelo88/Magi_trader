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
    get_pool_stats,
    record_bot_order,
    batch_insert_bot_logs,
    batch_insert_voter_feedback,
    batch_record_bot_decisions,
    fetch_bot_orders_chronological,
    get_bot_risk_state,
    update_bot_risk_state,
    upsert_ohlcv_candles,
)
from trading.app_settings import get_execution_mode, is_global_halt
from trading.bot_performance import compute_strategy_performance
from trading.exchange_factory import build_binance_spot, build_binance_public
from trading.risk_manager import evaluate_trade_risk
from trading.risk_settings import get_effective_bot_risk_settings
from trading.strategies.registry import get_strategy, default_params_for
from services.websocket_manager import publish_bot_event, ws_manager
from services.monitoring import monitor

_last_trade_monotonic: dict[str, float] = {}

POLL_SEC = 5

_cycle_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="bot-cycle",
)
_cycle_seq_lock = threading.Lock()
_cycle_seq = 0
_auth_exchange_cache: dict[str, Any] = {}
_auth_exchange_cache_lock = threading.Lock()
_last_ohlcv_cache_upsert: dict[tuple[str, str], float] = {}
_last_ohlcv_cache_upsert_lock = threading.Lock()


def _slow_cycle_breakdown_threshold_ms() -> float:
    try:
        return float(os.environ.get("SLOW_CYCLE_BREAKDOWN_MS", "1500"))
    except ValueError:
        return 1500.0


def _slow_trade_breakdown_threshold_ms() -> float:
    try:
        return float(os.environ.get("SLOW_TRADE_BREAKDOWN_MS", "5000"))
    except ValueError:
        return 5000.0


def _slow_cycle_print_threshold_ms(trade_executed: bool) -> float:
    key = "SLOW_TRADE_CYCLE_MS" if trade_executed else "SLOW_NON_TRADE_CYCLE_MS"
    default = "5000" if trade_executed else "1000"
    try:
        return float(os.environ.get(key, default))
    except ValueError:
        return float(default)


def _stall_debug_enabled() -> bool:
    raw = (os.environ.get("BOT_STALL_DEBUG_LOGS") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _next_cycle_seq() -> int:
    global _cycle_seq
    with _cycle_seq_lock:
        _cycle_seq += 1
        return _cycle_seq


def _cycle_debug(cycle_id: int, message: str) -> None:
    if not _stall_debug_enabled():
        return
    print(f"[bot_runner][cycle {cycle_id}] {message}", flush=True)


def _ohlcv_cache_upsert_interval_sec() -> float:
    try:
        return float(os.environ.get("OHLCV_CACHE_UPSERT_INTERVAL_SEC", "60"))
    except ValueError:
        return 60.0


def _should_upsert_ohlcv_cache(symbol: str, timeframe: str) -> bool:
    interval = _ohlcv_cache_upsert_interval_sec()
    if interval <= 0:
        return False
    key = (symbol, timeframe)
    now = time.monotonic()
    with _last_ohlcv_cache_upsert_lock:
        last = _last_ohlcv_cache_upsert.get(key, 0.0)
        if now - last < interval:
            return False
        _last_ohlcv_cache_upsert[key] = now
    return True


def min_running_bot_cooldown_remaining_sec() -> float | None:
    """Return seconds until the next running bot is due for active work.

    ``None`` means no running bots. ``0`` means at least one bot is due now or
    has not traded yet, so MetaMagi should use only tiny cooperative batches.
    """
    bots = _load_running_bots()
    if not bots:
        return None
    now = time.monotonic()
    remaining_values: list[float] = []
    for bot in bots:
        bot_id = bot["bot_id"]
        params = _merge_params(
            bot.get("strategy") or "sma_cross",
            bot.get("strategy_params_json"),
        )
        interval_key = float(params["min_trade_interval_sec"])
        last = _last_trade_monotonic.get(bot_id, 0.0)
        if last <= 0:
            remaining_values.append(0.0)
            continue
        remaining_values.append(max(0.0, interval_key - (now - last)))
    return min(remaining_values) if remaining_values else None


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
    print(
        "[bot_runner] Decision Optimization + Parallelization Improvements ENABLED",
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


def _log_slow_full_cycle_breakdown(
    bot_count: int,
    phase_ms: dict[str, float],
    total_ms: float,
) -> None:
    """Terminal-visible timing split for work outside each bot's `_process_bot`."""
    ordered = (
        ("load_bots_ms", "Load bots"),
        ("exchange_setup_ms", "Exchange setup"),
        ("params_ms", "Params/cooldown"),
        ("market_prefetch_ms", "Market metadata"),
        ("balance_prefetch_ms", "Balance prefetch"),
        ("position_prefetch_ms", "Position prefetch"),
        ("features_prefetch_ms", "Feature snapshot"),
        ("ohlcv_prefetch_ms", "OHLCV prefetch"),
        ("dispatch_ms", "Bot dispatch"),
        ("flush_ms", "DB/log flush"),
    )
    lines = [
        f"[SLOW FULL CYCLE BREAKDOWN] bots={bot_count}",
        *[
            f"  {label:<17}: {phase_ms.get(key, 0.0):6.0f}ms"
            for key, label in ordered
        ],
        f"  Total wall       : {total_ms:6.0f}ms",
    ]
    print("\n".join(lines), flush=True)


def _log_slow_trade_breakdown(
    bot_id: str,
    side: str,
    phase_ms: dict[str, float],
    total_ms: float,
) -> None:
    """Terminal-visible split for slow trade execution."""
    print(
        f"[SLOW TRADE BREAKDOWN] bot={bot_id} side={side}\n"
        f"  create_order       : {phase_ms.get('create_order_ms', 0.0):6.0f}ms\n"
        f"  record_order       : {phase_ms.get('record_order_ms', 0.0):6.0f}ms\n"
        f"  logs/events        : {phase_ms.get('events_ms', 0.0):6.0f}ms\n"
        f"  post_trade_balance : {phase_ms.get('post_balance_ms', 0.0):6.0f}ms\n"
        f"  total trade        : {total_ms:6.0f}ms",
        flush=True,
    )


_last_throttled_bot_info: dict[str, float] = {}
_last_throttled_print: dict[str, float] = {}
_last_thread_pool_stats_log: float = 0.0

# Testnet fetch_balance is routinely 2.5-4s. Keep the last wallet snapshot long
# enough to cover multiple 60s trade intervals; exchange order placement remains
# the final safety check if the external wallet changed meanwhile.
_BALANCE_CACHE_TTL_SEC = float(os.environ.get("BOT_BALANCE_CACHE_TTL_SEC", "300"))
_balance_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_balance_cache_lock = threading.Lock()

_POSITION_CACHE_TTL_SEC = float(os.environ.get("BOT_POSITION_CACHE_TTL_SEC", "60"))
_position_cache: dict[tuple[str, str], tuple[float, tuple[float, float]]] = {}
_position_cache_lock = threading.Lock()

_FEATURES_SNAPSHOT_MISSING = object()

# Per-cycle log queue — all bot_log rows written during one _run_one_cycle()
# call are collected here and flushed in a single batch transaction at the end.
# Protected by a lock because multiple bot threads append concurrently.
_log_queue: list[tuple] = []
_log_queue_lock = threading.Lock()
_decision_queue: list[dict[str, Any]] = []
_decision_queue_lock = threading.Lock()
_voter_feedback_queue: list[dict[str, Any]] = []
_voter_feedback_queue_lock = threading.Lock()
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
        print(
            f"[bot_runner] publish_bot_event failed "
            f"bot={bot_id} event={event_type}:\n{traceback.format_exc()}",
            flush=True,
        )


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
        print(
            f"[bot_runner] get_features_snapshot failed symbol={symbol}:\n"
            f"{traceback.format_exc()}",
            flush=True,
        )
    return None


def _log_voter_feedback(
    bot_id: str,
    symbol: str,
    result: Any,
    features_snapshot: str | None | object = _FEATURES_SNAPSHOT_MISSING,
) -> None:
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
    # Capture the current market microstructure snapshot once per cycle per
    # symbol. If the cycle did not prefetch it, fall back to the old direct read.
    if features_snapshot is _FEATURES_SNAPSHOT_MISSING:
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
        _queue_voter_feedback(records)
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
    except Exception as e:
        print(
            f"[bot_runner] voter feedback queue/event failed "
            f"bot={bot_id} symbol={symbol}: {e}\n{traceback.format_exc()}",
            flush=True,
        )


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
        print(
            f"[bot_runner] batch_insert_bot_logs failed rows={len(rows)}:\n"
            f"{traceback.format_exc()}",
            flush=True,
        )


def _queue_global_decision(decision: dict[str, Any]) -> None:
    with _decision_queue_lock:
        _decision_queue.append(decision)


def _flush_decision_queue() -> None:
    """Persist all bot decisions for the cycle in one DB transaction."""
    with _decision_queue_lock:
        if not _decision_queue:
            return
        rows = list(_decision_queue)
        _decision_queue.clear()
    try:
        batch_record_bot_decisions(rows)
    except Exception as e:
        print(
            f"[bot_runner] batch_record_bot_decisions failed "
            f"rows={len(rows)}: {e}\n{traceback.format_exc()}",
            flush=True,
        )


def _queue_voter_feedback(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with _voter_feedback_queue_lock:
        _voter_feedback_queue.extend(records)


def _flush_voter_feedback_queue() -> None:
    """Persist queued voter feedback after bot workers finish their hot path."""
    with _voter_feedback_queue_lock:
        if not _voter_feedback_queue:
            return
        rows = list(_voter_feedback_queue)
        _voter_feedback_queue.clear()
    try:
        batch_insert_voter_feedback(rows)
    except Exception as e:
        print(
            f"[bot_runner] batch_insert_voter_feedback failed "
            f"rows={len(rows)}: {e}\n{traceback.format_exc()}",
            flush=True,
        )


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


def _pause_bot_for_risk(bot_id: str, execution_mode: str, reason: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE bots SET status = 'paused' WHERE bot_id = ?", (bot_id,))
        conn.commit()
    finally:
        conn.close()
    update_bot_risk_state(bot_id, {"last_risk_pause_reason": reason})
    _log(bot_id, "warn", execution_mode, f"Risk protection paused bot — {reason}")
    _emit_bot_event(
        bot_id,
        "bot_status_changed",
        {"status": "paused", "reason": reason, "source": "risk_manager"},
        priority=True,
    )


def _stop_bot_for_risk(bot_id: str, execution_mode: str, reason: str) -> None:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE bots SET status = 'stopped' WHERE bot_id = ?", (bot_id,))
        conn.commit()
    finally:
        conn.close()
    update_bot_risk_state(bot_id, {"last_risk_stop_reason": reason})
    _log(bot_id, "warn", execution_mode, f"Risk protection stopped bot — {reason}")
    _emit_bot_event(
        bot_id,
        "bot_status",
        {"status": "stopped", "reason": reason, "source": "risk_manager"},
        priority=True,
    )


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
        _cache_balance(execution_mode, bal)
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
        print(
            f"[bot_runner] post-trade balance refresh failed "
            f"bot={bot_id} mode={execution_mode}:\n{traceback.format_exc()}",
            flush=True,
        )


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
    except Exception as e:
        print(
            f"[bot_runner] position calculation failed "
            f"bot={bot_id} symbol={symbol}: {e}\n{traceback.format_exc()}",
            flush=True,
        )
        return 0.0, 0.0


def _invalidate_position_cache(bot_id: str, symbol: str) -> None:
    with _position_cache_lock:
        _position_cache.pop((bot_id, symbol), None)


def _get_cached_bot_position(bot_id: str, symbol: str) -> tuple[float, float]:
    """Short-lived position cache; invalidated whenever this runner records an order."""
    key = (bot_id, symbol)
    now = time.monotonic()
    with _position_cache_lock:
        cached = _position_cache.get(key)
        if cached and now - cached[0] <= _POSITION_CACHE_TTL_SEC:
            return cached[1]
    pos = _get_bot_position(bot_id, symbol)
    with _position_cache_lock:
        _position_cache[key] = (now, pos)
    return pos


def _get_cached_balance(mode: str, ex: Any) -> dict[str, Any] | None:
    """Cache wallet snapshots briefly to avoid blocking every Decision on CCXT."""
    now = time.monotonic()
    with _balance_cache_lock:
        cached = _balance_cache.get(mode)
        if cached and now - cached[0] <= _BALANCE_CACHE_TTL_SEC:
            return cached[1]
    try:
        balance = ex.fetch_balance()
    except Exception as e:
        print(
            f"[bot_runner] fetch_balance cache refresh failed "
            f"mode={mode}: {e}\n{traceback.format_exc()}",
            flush=True,
        )
        return None
    with _balance_cache_lock:
        _balance_cache[mode] = (now, balance)
    return balance


def _peek_cached_balance(mode: str) -> dict[str, Any] | None:
    now = time.monotonic()
    with _balance_cache_lock:
        cached = _balance_cache.get(mode)
        if cached and now - cached[0] <= _BALANCE_CACHE_TTL_SEC:
            return cached[1]
    return None


def _cache_balance(mode: str, balance: dict[str, Any]) -> None:
    with _balance_cache_lock:
        _balance_cache[mode] = (time.monotonic(), balance)


def _amount_step_from_market(market: dict[str, Any]) -> tuple[float | None, int]:
    try:
        prec_val = market.get("precision", {}).get("amount")
        if prec_val is None:
            return None, 0
        pv = float(prec_val)
        if pv <= 0:
            return None, 0
        step = pv if pv < 1 else 10.0 ** (-int(pv))
        return step, max(0, round(-math.log10(step)))
    except Exception:
        return None, 0


def _ceil_amount_to_min_notional(
    market: dict[str, Any],
    min_notional: float,
    last_close: float,
) -> float | None:
    """Return the smallest lot-size amount that clears min_notional."""
    if min_notional <= 0 or last_close <= 0:
        return None
    step, precision = _amount_step_from_market(market)
    if not step:
        return None
    min_qty = min_notional / last_close
    return round(math.ceil(round(min_qty / step, 9)) * step, precision)


def _process_bot(
    ex,
    bot: dict[str, Any],
    execution_mode: str,
    prefetched_ohlcv: list | None = None,
    public_ex=None,
    phase_ms: dict[str, float] | None = None,
    market_cache: dict[str, dict[str, Any]] | None = None,
    balance_cache: dict[str, dict[str, Any]] | None = None,
    position_cache: dict[tuple[str, str], tuple[float, float]] | None = None,
    features_cache: dict[str, str | None] | None = None,
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

    def _queue_decision(action: str, confidence: float | None, executed: bool) -> None:
        _queue_global_decision({
            "bot_id": bot_id,
            "symbol": symbol,
            "mode": execution_mode,
            "action": action,
            "confidence": confidence,
            "executed": executed,
        })

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
    features_snapshot = (
        features_cache.get(symbol)
        if features_cache is not None and symbol in features_cache
        else _FEATURES_SNAPSHOT_MISSING
    )
    _log_voter_feedback(bot_id, symbol, result, features_snapshot)

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
        _close_decision()
        return

    meta_str = _format_meta(result.meta)
    _log(bot_id, "info", execution_mode, f"Signal: {result.signal.upper()} — {meta_str}")

    _signal_confidence: float | None = result.confidence

    try:
        market = market_cache.get(symbol) if market_cache is not None else None
        if market is None:
            _log(bot_id, "warn", execution_mode, "Decision cache miss: market metadata")
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

    balance = balance_cache.get(execution_mode) if balance_cache is not None else None
    if balance is None:
        balance = _peek_cached_balance(execution_mode)
    if balance is None:
        _log(bot_id, "warn", execution_mode, "Decision cache miss: wallet balance — refreshing from exchange")
        balance = _get_cached_balance(execution_mode, ex)
    if balance is None:
        _log(bot_id, "error", execution_mode, "fetch_balance failed")
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

    consensus_score: float | None = None
    if isinstance(result.meta, dict):
        raw_score = result.meta.get("consensus_score")
        try:
            consensus_score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            consensus_score = None
    if consensus_score is None:
        consensus_score = _signal_confidence

    initial_budget_for_risk = float(params.get("initial_budget_quote") or 0)
    if initial_budget_for_risk <= 0:
        initial_budget_for_risk = max(0.0, free_quote)
    try:
        risk_settings = get_effective_bot_risk_settings(bot_id)
        orders_for_risk = fetch_bot_orders_chronological(bot_id)
        risk_state = get_bot_risk_state(bot_id)
        risk_decision = evaluate_trade_risk(
            settings=risk_settings,
            orders_oldest_first=orders_for_risk,
            symbol=symbol,
            initial_capital=initial_budget_for_risk,
            mark_price=last_close,
            consensus_score=consensus_score,
            ohlcv=ohlcv,
            side=str(result.signal),
            risk_state=risk_state,
        )
    except Exception as e:
        _log(bot_id, "error", execution_mode, f"Risk check failed: {e}")
        _close_decision()
        return

    if not risk_decision.allowed:
        reason = risk_decision.reason or "risk protection triggered"
        if risk_decision.should_stop:
            _stop_bot_for_risk(bot_id, execution_mode, reason)
        elif risk_decision.should_pause:
            _pause_bot_for_risk(bot_id, execution_mode, reason)
        else:
            _log(bot_id, "warn", execution_mode, f"Trade skipped — {reason}")
        _queue_decision(str(result.signal).upper(), _signal_confidence, False)
        _close_decision()
        return

    _log(
        bot_id,
        "info",
        execution_mode,
        "Risk: "
        f"capital={risk_decision.current_capital or initial_budget_for_risk:.4f} {quote_cur} "
        f"risk={risk_decision.risk_pct or 0.0:.2f}% "
        f"size_mult={risk_decision.size_multiplier:.2f} "
        f"dd={risk_decision.drawdown_pct or 0.0:.2f}% "
        f"daily_pnl={risk_decision.daily_pnl or 0.0:.4f}",
    )

    if result.signal == "buy":
        initial_budget = float(params.get("initial_budget_quote") or 0)
        risk_pct = float(risk_decision.risk_pct or 0.0)
        capital_ref = max(0.0, float(risk_decision.current_capital or initial_budget_for_risk))
        target_spend = capital_ref * (risk_pct / 100.0) * float(risk_decision.size_multiplier)

        if initial_budget > 0:
            # Budget-constrained: only trade within the configured budget
            _, open_cost = (
                position_cache.get((bot_id, symbol))
                if position_cache is not None and (bot_id, symbol) in position_cache
                else _get_cached_bot_position(bot_id, symbol)
            )
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
            spend = min(target_spend, remaining_budget, free_quote)
            if spend < min_spend:
                spend = min_spend
            _log(bot_id, "info", execution_mode,
                 f"Budget: {remaining_budget:.4f} {quote_cur} remaining of {initial_budget:.2f} {quote_cur} budget")
        else:
            # No budget configured — use exchange wallet as fallback
            min_spend = min_cost * 1.02 if min_cost > 0 else 0.0
            spend = min(target_spend, free_quote)
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
        qty_prec = float(data_ex.amount_to_precision(symbol, qty_raw))

        # If lot-size rounding dropped the notional below the buffered minimum,
        # ceiling-round the quantity up to the next valid lot step.
        _min_notional = (min_cost * 1.02) if min_cost > 0 else 0.0
        if _min_notional > 0 and qty_prec * last_close < _min_notional:
            ceiled_qty = _ceil_amount_to_min_notional(market, _min_notional, last_close)
            if ceiled_qty is not None:
                qty_prec = ceiled_qty

        if qty_prec <= 0:
            _log(bot_id, "warn", execution_mode, "BUY skipped — quantity rounds to zero")
            _close_decision()
            return

        actual_spend = qty_prec * last_close
        budget_ref = remaining_budget if initial_budget > 0 else free_quote
        _log(bot_id, "info", execution_mode,
             f"BUY order: spending ~{actual_spend:.4f} {quote_cur} "
             f"({risk_pct:.2f}% risk of {capital_ref:.4f} {quote_cur}; "
             f"cap_ref={budget_ref:.4f}) @ ~{last_close:.2f}")
        _close_decision()
        t_trade0 = time.perf_counter()
        trade_phase_ms: dict[str, float] = {}
        try:
            t_sub = time.perf_counter()
            order = ex.create_order(symbol, "market", "buy", qty_prec)
            trade_phase_ms["create_order_ms"] = (time.perf_counter() - t_sub) * 1000.0
            _last_trade_monotonic[bot_id] = time.monotonic()
            t_sub = time.perf_counter()
            record_bot_order(bot_id, execution_mode, order)
            _invalidate_position_cache(bot_id, symbol)
            trade_phase_ms["record_order_ms"] = (time.perf_counter() - t_sub) * 1000.0
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost")) or actual_spend
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            t_sub = time.perf_counter()
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
            trade_phase_ms["events_ms"] = (time.perf_counter() - t_sub) * 1000.0
            t_sub = time.perf_counter()
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            trade_phase_ms["post_balance_ms"] = (time.perf_counter() - t_sub) * 1000.0
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
            if timings["trade_ms"] > _slow_trade_breakdown_threshold_ms():
                _log_slow_trade_breakdown(bot_id, "buy", trade_phase_ms, timings["trade_ms"])

    elif result.signal == "sell":
        base_fraction = float(params["base_fraction"])
        initial_budget = float(params.get("initial_budget_quote") or 0)

        if initial_budget > 0:
            # Sell only the base position this bot acquired with its own budget
            open_base, _ = (
                position_cache.get((bot_id, symbol))
                if position_cache is not None and (bot_id, symbol) in position_cache
                else _get_cached_bot_position(bot_id, symbol)
            )
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

        sell_prec = data_ex.amount_to_precision(symbol, sell_amt)
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
            ceiled_qty = _ceil_amount_to_min_notional(market, min_cost, last_close)
            if ceiled_qty is not None:
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
            if not ceiled:
                bumped_amt = (min_cost * 1.02) / last_close
                if bumped_amt <= available:
                    sell_prec = data_ex.amount_to_precision(symbol, bumped_amt)
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
        trade_phase_ms: dict[str, float] = {}
        try:
            t_sub = time.perf_counter()
            order = ex.create_order(symbol, "market", "sell", float(sell_prec))
            trade_phase_ms["create_order_ms"] = (time.perf_counter() - t_sub) * 1000.0
            _last_trade_monotonic[bot_id] = time.monotonic()
            t_sub = time.perf_counter()
            record_bot_order(bot_id, execution_mode, order)
            _invalidate_position_cache(bot_id, symbol)
            trade_phase_ms["record_order_ms"] = (time.perf_counter() - t_sub) * 1000.0
            filled_base = _f_order(order.get("filled"))
            cost_quote = _f_order(order.get("cost"))
            avg_price = _f_order(order.get("average")) or (cost_quote / filled_base if filled_base > 0 else 0.0)
            t_sub = time.perf_counter()
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
            trade_phase_ms["events_ms"] = (time.perf_counter() - t_sub) * 1000.0
            t_sub = time.perf_counter()
            _log_post_trade_balance(bot_id, execution_mode, ex, base_cur, quote_cur)
            trade_phase_ms["post_balance_ms"] = (time.perf_counter() - t_sub) * 1000.0
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
            if timings["trade_ms"] > _slow_trade_breakdown_threshold_ms():
                _log_slow_trade_breakdown(bot_id, "sell", trade_phase_ms, timings["trade_ms"])

    else:
        # Strategies should only emit hold / buy / sell; close the slice anyway.
        _close_decision()

    # Decisions are flushed once per cycle after bot worker threads complete.


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
        with _auth_exchange_cache_lock:
            cached = _auth_exchange_cache.get(mode)
        if cached is not None:
            exchanges[mode] = cached
            continue
        try:
            t0 = time.perf_counter()
            ex = build_binance_spot(mode)
            # ccxt timeout is in milliseconds.  15 s is generous for testnet
            # but still short enough that a dead connection unblocks quickly.
            ex.timeout = 15_000
            try:
                ex.load_markets()
            except Exception:
                print(
                    f"[bot_runner] Auth exchange market preload failed "
                    f"mode={mode}:\n{traceback.format_exc()}",
                    flush=True,
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            print(
                f"[bot_runner] Auth exchange ready mode={mode} "
                f"markets_loaded={bool(getattr(ex, 'markets', None))} "
                f"elapsed={elapsed_ms:.0f}ms",
                flush=True,
            )
            with _auth_exchange_cache_lock:
                _auth_exchange_cache[mode] = ex
            exchanges[mode] = ex
        except ValueError as e:
            print(f"[bot_runner] Exchange config error for mode={mode}: {e}", flush=True)
        except Exception:
            print(
                f"[bot_runner] Exchange setup failed for mode={mode}:\n"
                f"{traceback.format_exc()}",
                flush=True,
            )
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
                loop.run_in_executor(_cycle_executor, _run_one_cycle),
                timeout=120.0,
            )
        except asyncio.TimeoutError:
            print(
                "[bot_runner] Full cycle exceeded 120 s wall time — "
                "the single cycle executor may be stuck behind a blocking call.",
                flush=True,
            )
        except Exception:
            print(
                f"[bot_runner] run_async loop error:\n{traceback.format_exc()}",
                flush=True,
            )
        await asyncio.sleep(POLL_SEC)


_public_exchange = None  # lazily initialised mainnet read-only instance


def _get_public_exchange():
    """Return (or create) the shared unauthenticated mainnet exchange for OHLCV."""
    global _public_exchange
    if _public_exchange is None:
        _public_exchange = build_binance_public()
    return _public_exchange


def _thread_pool_stats_interval_sec() -> float:
    try:
        return float(os.environ.get("THREAD_POOL_STATS_INTERVAL_SEC", "300"))
    except ValueError:
        return 300.0


def _maybe_log_thread_pool_stats(
    *,
    running_bots: int,
    max_workers: int,
    submitted: int,
    done: int,
    timed_out: int,
) -> None:
    """Log executor and DB-pool utilization periodically."""
    global _last_thread_pool_stats_log
    now = time.monotonic()
    if now - _last_thread_pool_stats_log < _thread_pool_stats_interval_sec():
        return
    _last_thread_pool_stats_log = now
    active = max(0, submitted - done)
    idle = max(0, max_workers - active)
    db_stats = get_pool_stats()
    print(
        "[bot_runner] Thread pool stats: "
        f"bots={running_bots} workers={max_workers} submitted={submitted} "
        f"done={done} active={active} idle={idle} timed_out={timed_out} | "
        f"db_pool={db_stats}",
        flush=True,
    )


def _run_one_cycle():
    """One polling cycle — extracted so both the async and sync entrypoints share it."""
    cycle_id = _next_cycle_seq()
    _cycle_wall_t0 = time.perf_counter()
    _cycle_debug(cycle_id, "start")
    full_phase_ms: dict[str, float] = {
        "load_bots_ms": 0.0,
        "exchange_setup_ms": 0.0,
        "params_ms": 0.0,
        "market_prefetch_ms": 0.0,
        "balance_prefetch_ms": 0.0,
        "position_prefetch_ms": 0.0,
        "features_prefetch_ms": 0.0,
        "ohlcv_prefetch_ms": 0.0,
        "dispatch_ms": 0.0,
        "flush_ms": 0.0,
    }

    if is_global_halt():
        _cycle_debug(cycle_id, "global halt")
        _throttled_print(
            "global_halt",
            "[bot_runner] Waiting: global trading halt is ON — no bot cycles.",
        )
        return

    _cycle_debug(cycle_id, "load_bots begin")
    t_phase = time.perf_counter()
    bots = _load_running_bots()
    full_phase_ms["load_bots_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"load_bots done bots={len(bots)} elapsed={full_phase_ms['load_bots_ms']:.0f}ms",
    )
    if not bots:
        _throttled_print(
            "no_running_bots",
            f"[bot_runner] Waiting: no bots with status=running in DB (sleep {POLL_SEC}s).",
        )
        _cycle_debug(cycle_id, "end no running bots")
        return

    # Authenticated exchanges (per execution_mode) — used only for balance + orders.
    _cycle_debug(cycle_id, "exchange_setup begin")
    t_phase = time.perf_counter()
    exchanges = _build_exchanges_for_bots(bots)
    # Single unauthenticated mainnet exchange — used for OHLCV + market metadata.
    public_ex = _get_public_exchange()
    full_phase_ms["exchange_setup_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"exchange_setup done modes={sorted(exchanges)} "
        f"elapsed={full_phase_ms['exchange_setup_ms']:.0f}ms",
    )

    _cycle_debug(cycle_id, "params/cooldown begin")
    t_phase = time.perf_counter()
    params_by_bot_id: dict[str, dict[str, Any]] = {}
    active_bots: list[dict[str, Any]] = []
    for bot in bots:
        params = _merge_params(bot.get("strategy") or "sma_cross", bot.get("strategy_params_json"))
        params_by_bot_id[bot["bot_id"]] = params
        interval_key = float(params["min_trade_interval_sec"])
        if time.monotonic() - _last_trade_monotonic.get(bot["bot_id"], 0.0) >= interval_key:
            active_bots.append(bot)
    full_phase_ms["params_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"params/cooldown done active={len(active_bots)} "
        f"elapsed={full_phase_ms['params_ms']:.0f}ms",
    )

    # Per-cycle snapshots keep the Decision phase focused on in-memory checks.
    _cycle_debug(cycle_id, "market_metadata begin")
    t_phase = time.perf_counter()
    market_cache: dict[str, dict[str, Any]] = {}
    try:
        if not public_ex.markets:
            public_ex.load_markets()
        for symbol in {bot["symbol"] for bot in active_bots}:
            market_cache[symbol] = public_ex.market(symbol)
    except Exception:
        print(
            f"[bot_runner] market metadata prefetch failed:\n{traceback.format_exc()}",
            flush=True,
        )
    full_phase_ms["market_prefetch_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"market_metadata done markets={len(market_cache)} "
        f"elapsed={full_phase_ms['market_prefetch_ms']:.0f}ms",
    )

    _cycle_debug(cycle_id, "balance_prefetch skipped")
    t_phase = time.perf_counter()
    balance_cache: dict[str, dict[str, Any]] = {}
    # Do not eagerly call fetch_balance here. Most cycles are HOLD, and wallet
    # reads are the slowest exchange call in testnet (~2.6-3.3s). BUY/SELL paths
    # lazily use `_get_cached_balance()` only after a strategy emits a trade.
    full_phase_ms["balance_prefetch_ms"] = (time.perf_counter() - t_phase) * 1000.0

    _cycle_debug(cycle_id, "position_prefetch skipped")
    t_phase = time.perf_counter()
    position_cache: dict[tuple[str, str], tuple[float, float]] = {}
    # Position replay is only needed for budget-constrained BUY/SELL decisions.
    # Keep it lazy so HOLD cycles avoid DB order-history reads entirely.
    full_phase_ms["position_prefetch_ms"] = (time.perf_counter() - t_phase) * 1000.0

    _cycle_debug(cycle_id, "features_snapshot begin")
    t_phase = time.perf_counter()
    features_cache: dict[str, str | None] = {
        symbol: _get_features_snapshot(symbol)
        for symbol in {bot["symbol"] for bot in active_bots}
    }
    full_phase_ms["features_prefetch_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"features_snapshot done symbols={len(features_cache)} "
        f"elapsed={full_phase_ms['features_prefetch_ms']:.0f}ms",
    )

    # ── Pre-fetch OHLCV once per unique (symbol, timeframe, limit) ────────────
    # All bots share the same mainnet candle data regardless of their execution_mode,
    # so signals are always computed from real market prices.
    _cycle_debug(cycle_id, "ohlcv_prefetch begin")
    t_phase = time.perf_counter()
    ohlcv_cache: dict[tuple, list | None] = {}
    for bot in bots:
        params = params_by_bot_id[bot["bot_id"]]
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
                # Hard timeout on the network call so one slow exchange response
                # can't hold up the entire pre-fetch loop.
                public_ex.timeout = 15_000  # milliseconds (ccxt option)
                candles = public_ex.fetch_ohlcv(
                    symbol, timeframe=timeframe, limit=limit
                )
                ohlcv_cache[cache_key] = candles
                # Persist candles for backtesting replay on a throttle so live
                # trading does not add a DB writer every 5-second bot cycle.
                if _should_upsert_ohlcv_cache(symbol, timeframe):
                    upsert_ohlcv_candles(symbol, timeframe, candles)
            except Exception:
                print(
                    f"[bot_runner] pre-fetch OHLCV failed {symbol} {timeframe}:\n"
                    f"{traceback.format_exc()}",
                    flush=True,
                )
                ohlcv_cache[cache_key] = None  # bot will fall back to its own fetch
    full_phase_ms["ohlcv_prefetch_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"ohlcv_prefetch done keys={len(ohlcv_cache)} "
        f"elapsed={full_phase_ms['ohlcv_prefetch_ms']:.0f}ms",
    )

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
                market_cache=market_cache,
                balance_cache=balance_cache,
                position_cache=position_cache,
                features_cache=features_cache,
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
        trade_ms = float(phase_ms.get("trade_ms") or 0.0)
        trade_executed = trade_ms > 0.0
        monitor.record_bot_cycle(
            bot_id,
            cycle_ms,
            trade_executed=trade_executed,
            trade_ms=trade_ms,
        )

        thresh = _slow_cycle_breakdown_threshold_ms()
        if cycle_ms > thresh:
            _log_slow_cycle_breakdown(bot_id, phase_ms, cycle_ms)

        print_thresh = _slow_cycle_print_threshold_ms(trade_executed)
        if cycle_ms > print_thresh:
            print(
                f"[bot_runner] SLOW CYCLE bot={bot_id} symbol={symbol} "
                f"kind={'trade' if trade_executed else 'non_trade'} "
                f"→ {cycle_ms:.0f} ms (threshold={print_thresh:.0f}ms)",
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

    max_workers = min(len(bots) * 2, 64)
    _cycle_debug(
        cycle_id,
        f"dispatch begin bots={len(bots)} workers={max_workers}",
    )
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="bot-worker",
    )
    futures: dict[concurrent.futures.Future, str] = {
        pool.submit(_run_bot, bot): bot["bot_id"] for bot in bots
    }
    done: set[concurrent.futures.Future] = set()
    not_done: set[concurrent.futures.Future] = set(futures)
    t_phase = time.perf_counter()
    try:
        done, not_done = concurrent.futures.wait(
            futures, timeout=_CYCLE_TIMEOUT_SEC
        )
        if not_done:
            bot_ids = ", ".join(futures[f] for f in not_done)
            print(
                f"[bot_runner] Cycle timed out after {_CYCLE_TIMEOUT_SEC}s — "
                f"{len(not_done)} bot(s) still running (network hang?): {bot_ids}",
                flush=True,
            )
        for f in done:
            try:
                f.result()
            except Exception:
                print(
                    f"[bot_runner] Bot worker future failed "
                    f"bot={futures.get(f, '<unknown>')}:\n{traceback.format_exc()}",
                    flush=True,
                )
    finally:
        # Non-blocking shutdown: don't wait for hung threads.
        pool.shutdown(wait=False)
    full_phase_ms["dispatch_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"dispatch done done={len(done)} timed_out={len(not_done)} "
        f"elapsed={full_phase_ms['dispatch_ms']:.0f}ms",
    )

    _maybe_log_thread_pool_stats(
        running_bots=len(bots),
        max_workers=max_workers,
        submitted=len(futures),
        done=len(done),
        timed_out=len(not_done),
    )

    # Flush queued write-heavy telemetry after worker hot paths complete.
    _cycle_debug(cycle_id, "flush begin")
    t_phase = time.perf_counter()
    _flush_voter_feedback_queue()
    _flush_decision_queue()

    # Flush all bot_log rows collected during this cycle in one transaction.
    # Runs after the pool exits — by this point all *finished* threads have
    # appended their rows; orphaned threads may add a few straggler rows to
    # the next cycle's flush, which is acceptable.
    _flush_log_queue()
    full_phase_ms["flush_ms"] = (time.perf_counter() - t_phase) * 1000.0
    _cycle_debug(
        cycle_id,
        f"flush done elapsed={full_phase_ms['flush_ms']:.0f}ms",
    )

    # ── Observability ─────────────────────────────────────────────────────────
    wall_ms = (time.perf_counter() - _cycle_wall_t0) * 1000
    if wall_ms > 1000:
        _log_slow_full_cycle_breakdown(len(bots), full_phase_ms, wall_ms)
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
    _cycle_debug(cycle_id, f"end wall={wall_ms:.0f}ms")


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
