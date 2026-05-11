import json
import logging
import math
import sys
import os
import subprocess
import threading
import time
from contextlib import asynccontextmanager

from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# Configure structured logging before anything else so all loggers
# (monitoring, data_collector, meta_training_loop, etc.) emit to stdout.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)

from database import (
    create_bot,
    delete_bot,
    get_db_connection,
    get_bot_risk_settings,
    get_latest_voter_signals,
    get_pool_stats,
    init_db,
    fetch_bot_orders_panel,
    fetch_bot_orders_chronological,
    sync_bot_orders_from_logs,
    refresh_stale_bot_orders_from_exchange,
    fork_bot,
    metamagi_label_voter_feedback_catchup,
    peer_voter_weights_for_new_ensemble_bot,
    set_bot_execution_mode,
    update_bot_risk_state,
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
from trading.bot_performance import compute_strategy_performance, compute_closed_trades
from trading.strategy_budget import (
    initial_budget_from_strategy_params_json,
    merge_strategy_params_json,
    parse_initial_budget_api_value,
)
from trading.risk_settings import (
    db_row_to_risk_settings,
    ensure_bot_risk_settings,
    get_effective_bot_risk_settings,
    get_global_risk_defaults,
    save_bot_risk_settings,
    set_global_risk_defaults,
    template_risk_defaults,
)
from trading.risk_manager import risk_resume_state
from trading.strategies.registry import (
    get_strategy,
    strategy_names,
    strategy_catalog,
)
from services.websocket_manager import publish_bot_event, publish_bots_event, ws_manager
from services.monitoring import monitor as perf_monitor

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.abspath(os.path.join(_backend_dir, ".."))
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)

_METAMAGI_LABELED_EXPORT_SCRIPT = os.path.join(_repo_root, "scripts", "metamagi_labeled_export.py")
# Subprocess must drain stdout while the child runs; otherwise large --json output fills the pipe
# and the child deadlocks (looks like a timeout). Allow env override for huge DBs.
_METAMAGI_EXPORT_TIMEOUT_SEC = max(30, int(os.getenv("METAMAGI_EXPORT_TIMEOUT_SEC", "300")))
_BLEND_EDGE_FRAC = 0.65
_BLEND_ACC_FRAC = 0.35
_WEIGHT_CLAMP_MIN = 0.5
_WEIGHT_CLAMP_MAX_MULT = 2.0


def _sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _coerce_positive_float(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def _compute_blended_voter_weights(edge: dict[str, Any], acc: dict[str, Any]) -> dict[str, float]:
    """65% suggested_edge_weights + 35% suggested_accuracy_weights, mean-renormalized + clamped."""
    voters = sorted(set(edge) | set(acc))
    raw: dict[str, float] = {}
    for v in voters:
        ev = _coerce_positive_float(edge.get(v)) if v in edge else None
        av = _coerce_positive_float(acc.get(v)) if v in acc else None
        if ev is None and av is None:
            continue
        if ev is None:
            raw[v] = av if av is not None else 1.0
        elif av is None:
            raw[v] = ev
        else:
            raw[v] = _BLEND_EDGE_FRAC * ev + _BLEND_ACC_FRAC * av
    if not raw:
        return {}
    avg = sum(raw.values()) / len(raw)
    if avg <= 0:
        return {v: 1.0 for v in raw}
    max_w = avg * _WEIGHT_CLAMP_MAX_MULT
    out: dict[str, float] = {}
    for v, val in raw.items():
        ratio = val / avg
        w = max(_WEIGHT_CLAMP_MIN, min(max_w, ratio))
        out[v] = round(w, 4)
    return out


def _merge_blended_voter_weights_into_params(
    existing_json: str | None,
    blended: dict[str, float],
) -> dict[str, Any]:
    merged = merge_strategy_params_json(existing_json if isinstance(existing_json, str) else None, {})
    voters = merged.get("voters")
    prev_w = merged.get("voter_weights")
    prev_map: dict[str, float] = {}
    if isinstance(prev_w, dict):
        for k, v in prev_w.items():
            fv = _coerce_positive_float(v)
            if fv is not None:
                prev_map[str(k)] = fv
    if isinstance(voters, list):
        out_w: dict[str, float] = {}
        for v in voters:
            if not isinstance(v, str):
                continue
            if v in blended:
                out_w[v] = blended[v]
            else:
                out_w[v] = prev_map.get(v, 1.0)
        merged["voter_weights"] = out_w
    else:
        merged["voter_weights"] = dict(blended)
    return merged


def _optimize_weights_sse(bot_id: str):
    log = logging.getLogger("optimize_weights")

    yield _sse_event(
        {
            "type": "log",
            "level": "info",
            "message": f"Starting MetaMagi weight optimization for bot {bot_id}…",
        }
    )

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT strategy_params_json FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            yield _sse_event({"type": "log", "level": "error", "message": "Bot not found."})
            yield _sse_event({"type": "done", "ok": False})
            return
        existing_snapshot = row["strategy_params_json"]
    finally:
        conn.close()

    yield _sse_event({"type": "log", "level": "info", "message": "Connecting to database…"})

    # Script argparse supports edge|accuracy|both only (not "blended"); JSON always includes both maps.
    cmd = [
        sys.executable,
        _METAMAGI_LABELED_EXPORT_SCRIPT,
        "--bot",
        bot_id,
        "--weight-method",
        "both",
        "--hours",
        "720",
        "--sample-rows",
        "0",
        "--json",
    ]
    yield _sse_event(
        {
            "type": "log",
            "level": "info",
            "message": (
                "Running: python scripts/metamagi_labeled_export.py "
                f"--bot {bot_id} --weight-method both --hours 720 --sample-rows 0 --json"
            ),
        }
    )

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=_repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as e:
        log.exception("Failed to spawn metamagi export")
        yield _sse_event({"type": "log", "level": "error", "message": f"Could not run export script: {e}"})
        yield _sse_event({"type": "done", "ok": False})
        return

    stderr_lines: list[str] = []
    stdout_chunks: list[str] = []

    def pump_stderr() -> None:
        assert proc.stderr is not None
        try:
            for line in iter(proc.stderr.readline, ""):
                stderr_lines.append(line.rstrip())
        except Exception:
            log.exception("stderr pump failed for metamagi export")

    def drain_stdout() -> None:
        assert proc.stdout is not None
        try:
            stdout_chunks.append(proc.stdout.read())
        except Exception:
            log.exception("stdout drain failed for metamagi export")

    th_err = threading.Thread(target=pump_stderr, daemon=True)
    th_out = threading.Thread(target=drain_stdout, daemon=True)
    th_out.start()
    th_err.start()

    rc = 0
    try:
        rc = proc.wait(timeout=_METAMAGI_EXPORT_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("metamagi export kill wait timed out for bot %s", bot_id)
        yield _sse_event(
            {
                "type": "log",
                "level": "error",
                "message": (
                    f"MetaMagi export timed out after {_METAMAGI_EXPORT_TIMEOUT_SEC} seconds "
                    "(set METAMAGI_EXPORT_TIMEOUT_SEC if your DB needs longer)."
                ),
            }
        )
        yield _sse_event({"type": "done", "ok": False})
        th_out.join(timeout=15)
        th_err.join(timeout=15)
        return

    th_out.join(timeout=120)
    th_err.join(timeout=30)
    stdout_text = stdout_chunks[0] if stdout_chunks else ""

    for sl in stderr_lines:
        if sl.strip():
            yield _sse_event({"type": "log", "level": "info", "message": sl})

    if rc != 0:
        yield _sse_event({"type": "log", "level": "error", "message": f"Export script exited with code {rc}."})
        tail = stdout_text.strip()
        if tail:
            yield _sse_event({"type": "log", "level": "error", "message": tail[:4000]})
        yield _sse_event({"type": "done", "ok": False})
        return

    try:
        payload = json.loads(stdout_text.strip())
    except json.JSONDecodeError as e:
        yield _sse_event({"type": "log", "level": "error", "message": f"Invalid JSON from export script: {e}"})
        yield _sse_event({"type": "done", "ok": False})
        return

    hints = payload.get("llm_hints") or {}
    blended_raw = hints.get("suggested_blended_weights")
    blended: dict[str, float]
    if isinstance(blended_raw, dict) and blended_raw:
        blended = {}
        for k, v in blended_raw.items():
            fv = _coerce_positive_float(v)
            if fv is not None:
                blended[str(k)] = round(fv, 4)
    else:
        edge = hints.get("suggested_edge_weights") or {}
        acc = hints.get("suggested_accuracy_weights") or {}
        if not isinstance(edge, dict) or not isinstance(acc, dict):
            yield _sse_event(
                {"type": "log", "level": "error", "message": "Export payload missing suggested edge/accuracy weight maps."}
            )
            yield _sse_event({"type": "done", "ok": False})
            return
        blended = _compute_blended_voter_weights(edge, acc)

    if not blended:
        yield _sse_event(
            {"type": "log", "level": "error", "message": "No blended weights computed (insufficient labeled voter data?)."}
        )
        yield _sse_event({"type": "done", "ok": False})
        return

    counts = payload.get("counts") or {}
    nt = int(counts.get("total_rows") or 0)
    nl = int(counts.get("labeled_forward_roc_30s") or 0)
    yield _sse_event(
        {"type": "log", "level": "info", "message": f"Analyzing {nt:,} labeled rows (last 720 hours)…"}
    )
    yield _sse_event(
        {
            "type": "log",
            "level": "info",
            "message": f"{nl:,} rows carry forward ROC (30s) labels in this window.",
        }
    )
    yield _sse_event(
        {
            "type": "log",
            "level": "info",
            "message": "Computing blended weights (65% edge + 35% accuracy)…",
        }
    )

    try:
        merged = _merge_blended_voter_weights_into_params(
            existing_snapshot if isinstance(existing_snapshot, str) else None,
            blended,
        )
        vw = merged.get("voter_weights") or {}
        if isinstance(vw, dict):
            n_applied = len(vw)
        else:
            n_applied = len(blended)
        yield _sse_event(
            {"type": "log", "level": "info", "message": f"Updated {n_applied} voter weights successfully."}
        )

        out_json = json.dumps(merged, default=str)
        conn_u = get_db_connection()
        try:
            cur_u = conn_u.cursor()
            cur_u.execute(
                "UPDATE bots SET strategy_params_json = ? WHERE bot_id = ?",
                (out_json, bot_id),
            )
            conn_u.commit()
        finally:
            conn_u.close()

        yield _sse_event({"type": "log", "level": "info", "message": "Done. New weights applied."})
        yield _sse_event(
            {
                "type": "done",
                "ok": True,
                "voter_weights": vw if isinstance(vw, dict) else {},
                "strategy_params": merged,
                "strategy_params_json": out_json,
            }
        )
    except Exception as e:
        log.exception("optimize_weights DB update failed for bot %s", bot_id)
        yield _sse_event({"type": "log", "level": "error", "message": str(e)})
        yield _sse_event({"type": "done", "ok": False})


async def _meta_training_loop() -> None:
    """
    Background task: labels voter_feedback forward returns and updates MetaMagi
    voter weights every 30 minutes.

    Uses only local SQLite data (market_ticks + voter_feedback) — zero Binance
    API calls.
    """
    import asyncio
    import logging

    from database import (
        get_voter_feedback_batch,
        label_voter_feedback_forward_roc_batch,
    )
    from services.bot_runner import min_running_bot_cooldown_remaining_sec
    from trading.metatrader import get_metatrader

    logger = logging.getLogger("meta_training_loop")
    metatrader = get_metatrader()

    LABEL_INTERVAL_SEC = float(os.getenv("METAMAGI_LABEL_INTERVAL_SEC", "30"))
    TRAIN_INTERVAL_SEC = float(
        os.getenv("METAMAGI_TRAIN_INTERVAL_SEC", "1800")
    )
    LABEL_LOOKBACK_MINUTES = int(
        os.getenv("METAMAGI_LABEL_LOOKBACK_MINUTES", "180")
    )
    LABEL_BATCH_SIZE = int(os.getenv("METAMAGI_LABEL_BATCH_SIZE", "50"))
    ACTIVE_LABEL_BATCH_SIZE = int(
        os.getenv("METAMAGI_ACTIVE_LABEL_BATCH_SIZE", "10")
    )
    LABEL_MAX_SECONDS = float(os.getenv("METAMAGI_LABEL_MAX_SECONDS", "2"))
    ACTIVE_LABEL_MAX_SECONDS = float(
        os.getenv("METAMAGI_ACTIVE_LABEL_MAX_SECONDS", "0.5")
    )
    LABEL_MAX_BATCHES = int(os.getenv("METAMAGI_LABEL_MAX_BATCHES", "10"))
    LABEL_BATCH_SLEEP_SEC = (
        float(os.getenv("METAMAGI_LABEL_BATCH_SLEEP_MS", "200")) / 1000.0
    )
    MIN_COOLDOWN_SEC = float(
        os.getenv("METAMAGI_MIN_BOT_COOLDOWN_REMAINING_SEC", "10")
    )
    DB_BUSY_TIMEOUT_MS = int(os.getenv("METAMAGI_DB_BUSY_TIMEOUT_MS", "250"))
    TRAINING_ENABLED = (
        os.getenv("METAMAGI_TRAINING_ENABLED", "0").strip().lower()
        in ("1", "true", "yes", "on")
    )
    last_train_mono = time.monotonic()
    training_disabled_logged = False

    while True:
        try:
            await asyncio.sleep(LABEL_INTERVAL_SEC)
            loop = asyncio.get_event_loop()
            run_started = time.monotonic()
            cooldown_remaining = min_running_bot_cooldown_remaining_sec()
            if cooldown_remaining is None:
                batch_size = LABEL_BATCH_SIZE
                max_seconds = LABEL_MAX_SECONDS
                cooldown_desc = "no_running_bots"
            elif cooldown_remaining >= MIN_COOLDOWN_SEC:
                batch_size = LABEL_BATCH_SIZE
                max_seconds = min(LABEL_MAX_SECONDS, cooldown_remaining * 0.5)
                cooldown_desc = f"cooldown={cooldown_remaining:.1f}s"
            else:
                # Still make progress beside always-on bots, but keep the
                # transaction tiny when a bot is due soon or no cooldown
                # exists.
                batch_size = ACTIVE_LABEL_BATCH_SIZE
                max_seconds = ACTIVE_LABEL_MAX_SECONDS
                cooldown_desc = f"bot_due_soon={cooldown_remaining:.1f}s"

            logger.info(
                "MetaMagi: label run starting batch_size=%d max_seconds=%.1f "
                "lookback=%dmin busy_timeout=%dms %s",
                batch_size,
                max_seconds,
                LABEL_LOOKBACK_MINUTES,
                DB_BUSY_TIMEOUT_MS,
                cooldown_desc,
            )
            batches = 0
            selected_total = 0
            updated_30s_total = 0
            updated_5m_total = 0
            stop_reason = "max_seconds"
            while batches < LABEL_MAX_BATCHES:
                if time.monotonic() - run_started >= max_seconds:
                    stop_reason = "max_seconds"
                    break
                batch_started = time.perf_counter()
                result = await loop.run_in_executor(
                    None,
                    lambda: label_voter_feedback_forward_roc_batch(
                        lookback_minutes=LABEL_LOOKBACK_MINUTES,
                        batch_size=batch_size,
                        busy_timeout_ms=DB_BUSY_TIMEOUT_MS,
                    ),
                )
                batches += 1
                selected = int(result.get("selected") or 0)
                updated_30s = int(result.get("updated_30s") or 0)
                updated_5m = int(result.get("updated_5m") or 0)
                elapsed_ms = float(result.get("elapsed_ms") or 0.0)
                selected_total += selected
                updated_30s_total += updated_30s
                updated_5m_total += updated_5m
                logger.info(
                    "MetaMagi: label batch done selected=%d "
                    "updated_30s=%d updated_5m=%d elapsed=%.0fms",
                    selected,
                    updated_30s,
                    updated_5m,
                    elapsed_ms,
                )
                if result.get("db_busy"):
                    stop_reason = "db_busy"
                    break
                if selected == 0:
                    stop_reason = "no_rows"
                    break
                if updated_30s == 0 and updated_5m == 0:
                    stop_reason = "no_updatable_rows"
                    break
                if (time.perf_counter() - batch_started) * 1000.0 > 500:
                    logger.warning(
                        "MetaMagi: slow label batch elapsed=%.0fms",
                        (time.perf_counter() - batch_started) * 1000.0,
                    )
                await asyncio.sleep(LABEL_BATCH_SLEEP_SEC)
            else:
                stop_reason = "max_batches"
            logger.info(
                "MetaMagi: label run stopped reason=%s batches=%d "
                "selected=%d updated_30s=%d updated_5m=%d",
                stop_reason,
                batches,
                selected_total,
                updated_30s_total,
                updated_5m_total,
            )

            if not TRAINING_ENABLED:
                if not training_disabled_logged:
                    logger.info(
                        "MetaMagi: training disabled "
                        "(set METAMAGI_TRAINING_ENABLED=1 to enable)."
                    )
                    training_disabled_logged = True
                continue

            if time.monotonic() - last_train_mono < TRAIN_INTERVAL_SEC:
                continue
            last_train_mono = time.monotonic()

            # Pull last 24 h of feedback (including rows labeled over time).
            t0 = time.perf_counter()
            batch = await loop.run_in_executor(
                None,
                lambda: get_voter_feedback_batch(hours=24),
            )
            batch_ms = (time.perf_counter() - t0) * 1000.0
            logger.info(
                "MetaMagi: fetched %d feedback rows in %.0fms.",
                len(batch),
                batch_ms,
            )
            if not batch:
                logger.info(
                    "MetaMagi: no voter_feedback data yet — skipping."
                )
                continue

            # 3. Update EMA-based voter weights.
            t0 = time.perf_counter()
            updated_weights = metatrader.train_step(batch)
            train_ms = (time.perf_counter() - t0) * 1000.0
            if updated_weights:
                weight_str = "  ".join(
                    f"{v}={w:.3f}" for v, w in sorted(updated_weights.items())
                )
                logger.info(
                    "MetaMagi: updated weights in %.0fms — %s",
                    train_ms,
                    weight_str,
                )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "MetaMagi training loop error — will retry next cycle."
            )


async def _event_loop_lag_monitor() -> None:
    """Log when the FastAPI event loop is blocked long enough to affect WS/API."""
    import asyncio
    import logging

    logger = logging.getLogger("event_loop_lag")
    interval = float(os.getenv("EVENT_LOOP_LAG_INTERVAL_SEC", "5"))
    warn_ms = float(os.getenv("EVENT_LOOP_LAG_WARN_MS", "1000"))
    expected = time.monotonic() + interval

    while True:
        try:
            await asyncio.sleep(interval)
            now = time.monotonic()
            lag_ms = max(0.0, (now - expected) * 1000.0)
            if lag_ms >= warn_ms:
                logger.warning(
                    "Event loop lag detected: %.0fms "
                    "(interval=%.1fs threshold=%.0fms)",
                    lag_ms,
                    interval,
                    warn_ms,
                )
            expected = now + interval
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Event loop lag monitor error — continuing.")


def _supervise_task(task: Any, name: str) -> None:
    """Print any unexpected background task exit to the terminal."""
    logger = logging.getLogger("lifespan")

    def _done(done_task: Any) -> None:
        if done_task.cancelled():
            logger.info("Background task cancelled: %s", name)
            return
        try:
            exc = done_task.exception()
        except Exception:
            logger.exception("Failed inspecting background task: %s", name)
            return
        if exc is not None:
            logger.error(
                "Background task crashed: %s",
                name,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return
        logger.warning("Background task exited unexpectedly without error: %s", name)

    task.add_done_callback(_done)


async def _db_cleanup_loop() -> None:
    """
    Background task: archive old rows from high-volume live tables once per day.

    Waits 1 hour after startup before the first run so it does not compete with
    the bot runner during the noisy initialisation period.  Subsequent runs fire
    every 24 hours.  Any exception is caught and logged — this task must never
    crash the server.
    """
    import asyncio
    import logging

    logger = logging.getLogger("db_cleanup_loop")

    # Defer the first run: let the server settle for an hour before touching the DB.
    await asyncio.sleep(3600)

    while True:
        try:
            logger.info("Daily DB cleanup starting…")
            loop = asyncio.get_event_loop()
            from services.db_cleanup import run_cleanup
            result = await loop.run_in_executor(None, run_cleanup)
            logger.info(
                "Daily DB cleanup done — voter_feedback=%d  bot_decisions=%d  "
                "bot_logs=%d  market_ticks=%d  vacuumed=%s",
                result.voter_feedback_moved,
                result.bot_decisions_moved,
                result.bot_logs_moved,
                result.market_ticks_moved,
                result.vacuumed,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("DB cleanup loop error — will retry in 24 h.")

        await asyncio.sleep(86_400)  # 24 hours


async def _watchdog_loop() -> None:
    """Background task: detect silent application hangs and log CRITICAL warnings.

    Reads the watchdog heartbeat from the performance monitor (updated every
    60 s by maybe_log_health).  If the heartbeat goes stale for longer than
    WATCHDOG_TIMEOUT_SEC the application is likely stuck; a CRITICAL log is
    emitted together with the full monitor snapshot so the cause is immediately
    visible in the terminal without any external tooling.

    The watchdog never interferes with normal operation — it only logs.
    """
    import asyncio
    import logging

    logger = logging.getLogger("watchdog")
    INTERVAL = int(os.getenv("WATCHDOG_INTERVAL_SEC", "30"))
    TIMEOUT = int(os.getenv("WATCHDOG_TIMEOUT_SEC", "90"))
    POOL_LOG_INTERVAL = 300  # log pool stats every 5 minutes

    # Allow the system to initialise before the first check.
    await asyncio.sleep(INTERVAL * 2)

    last_pool_log: float = 0.0

    while True:
        await asyncio.sleep(INTERVAL)
        try:
            status = perf_monitor.watchdog_status()
            elapsed = status.get("seconds_since_update")

            if elapsed is not None and elapsed > TIMEOUT:
                snap = perf_monitor.snapshot(
                    running_bots=status.get("last_bots", 0),
                    ws_clients=status.get("last_clients", 0),
                )
                logger.critical(
                    "[CRITICAL] WATCHDOG: Application may be stuck! "
                    "No health update for %d seconds.\n"
                    "Last state: %d bots | %d WS clients | "
                    "DB ops: %d (slow: %d)",
                    elapsed,
                    status.get("last_bots", 0),
                    status.get("last_clients", 0),
                    snap["db"]["ops_total"],
                    snap["db"]["slow_ops"],
                )
                logger.critical(
                    "WATCHDOG snapshot: %s", json.dumps(snap, default=str)
                )

            # ── Pool stats (every 5 minutes) ──────────────────────────────
            now = time.monotonic()
            if now - last_pool_log >= POOL_LOG_INTERVAL:
                last_pool_log = now
                pool = get_pool_stats()
                logger.info(
                    "DB pool stats — size=%d  idle=%d  active=%d  "
                    "acquired=%d  hits=%d  misses=%d",
                    pool.get("pool_size", 0),
                    pool.get("idle", 0),
                    pool.get("active", 0),
                    pool.get("total_acquired", 0),
                    pool.get("pool_hits", 0),
                    pool.get("pool_misses", 0),
                )

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watchdog loop error — continuing.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    import asyncio
    from services.bot_runner import run_async as _bot_runner_async
    from services.data_collector import run_async as _data_collector_async
    _lifespan_logger = logging.getLogger("lifespan")
    init_db()
    _lifespan_logger.info(
        "=== MagiTrader - DB Pooling + Watchdog Mode ACTIVE ==="
    )
    ws_manager.bind_loop(asyncio.get_running_loop())
    # Pause any bots that were left running — the user must explicitly start them.
    conn = get_db_connection()
    try:
        conn.execute("UPDATE bots SET status = 'paused' WHERE status = 'running'")
        conn.commit()
    finally:
        conn.close()
    bot_task = asyncio.create_task(_bot_runner_async(), name="bot_runner")
    collector_task = asyncio.create_task(_data_collector_async(), name="data_collector")
    meta_task = asyncio.create_task(_meta_training_loop(), name="meta_training_loop")
    cleanup_task = asyncio.create_task(_db_cleanup_loop(), name="db_cleanup_loop")
    watchdog_task = asyncio.create_task(_watchdog_loop(), name="watchdog_loop")
    lag_task = asyncio.create_task(_event_loop_lag_monitor(), name="event_loop_lag")
    for task, name in (
        (bot_task, "bot_runner"),
        (collector_task, "data_collector"),
        (meta_task, "meta_training_loop"),
        (cleanup_task, "db_cleanup_loop"),
        (watchdog_task, "watchdog_loop"),
        (lag_task, "event_loop_lag"),
    ):
        _supervise_task(task, name)
    try:
        yield
    finally:
        for t in (
            bot_task,
            collector_task,
            meta_task,
            cleanup_task,
            watchdog_task,
            lag_task,
        ):
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


async def _websocket_channel_loop(websocket: WebSocket, channel: str) -> None:
    await ws_manager.connect(websocket, channel)
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                message = {}
            if message.get("type") == "ping":
                await websocket.send_json(
                    {
                        "type": "pong",
                        "timestamp": int(time.time() * 1000),
                        "data": {"channel": channel},
                    }
                )
    except WebSocketDisconnect:
        pass
    finally:
        await ws_manager.disconnect(websocket, channel)


@app.websocket("/ws/bots")
async def ws_bots(websocket: WebSocket):
    """Real-time bot list/status/overview updates."""
    await _websocket_channel_loop(websocket, "bots")


@app.websocket("/ws/bot/{bot_id}")
async def ws_bot_detail(websocket: WebSocket, bot_id: str):
    """Real-time detail updates for one bot."""
    await _websocket_channel_loop(websocket, f"bot:{bot_id}")


@app.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    """Lightweight tracked-market ticker updates from the backend collector."""
    await _websocket_channel_loop(websocket, "market")


@app.get("/ws/health")
def ws_health():
    """Connection counts for WebSocket observability."""
    return ws_manager.health()


@app.get("/api/health")
def api_health():
    """Comprehensive runtime health: DB stats, WS clients, bot cycles, slow ops.

    Includes watchdog status (seconds since last health heartbeat) and
    connection pool statistics so hangs and pool exhaustion are immediately
    visible without any external tooling.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM bots WHERE status = 'running'")
        running_bots = cur.fetchone()[0]
    except Exception:
        running_bots = 0
    finally:
        conn.close()

    snap = perf_monitor.snapshot(
        running_bots=running_bots,
        ws_clients=ws_manager.connected_count(),
    )
    snap["ws_channels"] = ws_manager.health()["channels"]
    snap["db_path"] = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")
    )
    try:
        snap["db_size_mb"] = round(os.path.getsize(snap["db_path"]) / 1_048_576, 1)
    except OSError:
        snap["db_size_mb"] = None

    snap["watchdog"] = perf_monitor.watchdog_status()
    snap["db_pool"] = get_pool_stats()
    return snap


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

    # Serve from cache if fresh enough — avoids blocking the thread pool on
    # every page load / navigation when the UI polls rapidly.
    _now = time.time()
    cached_wallet = _wallet_cache.get(effective)
    if cached_wallet and _now - cached_wallet[1] < _WALLET_TTL_SEC:
        return cached_wallet[0]

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
        payload = {
            "balances": non_zero_balances,
            "wallet_view": effective,
            "execution_mode": bot_mode,
        }
        _wallet_cache[effective] = (payload, _now)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        # On error, return stale cache if available rather than a 502 that
        # blanks the entire Dashboard.
        if cached_wallet:
            return cached_wallet[0]
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
        "voter_feedback",
        "market_depth",
        "bots",
    ]
    table_counts: dict[str, int] = {}
    metamagi_counts: dict[str, int] = {}
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        for tbl in tables:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")  # noqa: S608 – table names are hard-coded
                table_counts[tbl] = cur.fetchone()[0]
            except Exception:
                table_counts[tbl] = 0
        try:
            cur.execute(
                "SELECT COUNT(*) FROM voter_feedback "
                "WHERE forward_roc_30s IS NULL OR forward_roc_5m IS NULL"
            )
            metamagi_counts["unlabeled_rows"] = cur.fetchone()[0]
            metamagi_counts["total_rows"] = table_counts.get("voter_feedback", 0)
        except Exception:
            metamagi_counts["unlabeled_rows"] = 0
            metamagi_counts["total_rows"] = table_counts.get("voter_feedback", 0)
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
        "metamagi": metamagi_counts,
    }


class MetamagiLabelCatchupBody(BaseModel):
    """Optional tuning for voter_feedback catch-up labeling."""

    lookback_days: float | None = Field(
        default=None,
        ge=0.001,
        le=3660.0,
        description=(
            "Restrict scan to the last N days. Omit for full-table scan "
            "(see METAMAGI_CATCHUP_LOOKBACK_MINUTES)."
        ),
    )
    max_seconds: float | None = Field(
        default=None,
        ge=1.0,
        description=(
            "Optional wall-clock cap for this request. Omit for no cap (runs until backlog "
            "is gone). Same as env METAMAGI_CATCHUP_MAX_SECONDS when unset."
        ),
    )


@app.post("/api/data/metamagi-label-catchup")
async def metamagi_label_catchup(
    body: MetamagiLabelCatchupBody | None = Body(default=None),
):
    """Drain voter_feedback ROC backlog; response is NDJSON (``application/x-ndjson``).

    Each line is a JSON object: ``start`` (initial row count), ``progress`` (after
    each batch), optional ``db_busy``, then ``done`` (full summary) or ``error``.
    """
    import asyncio

    log = logging.getLogger("metamagi_catchup")
    lb_days = body.lookback_days if body else None
    max_sec = body.max_seconds if body else None
    lookback_minutes = (
        int(round(lb_days * 24 * 60)) if lb_days is not None else None
    )

    log.info(
        "HTTP POST /api/data/metamagi-label-catchup (NDJSON stream) "
        "lookback_days=%s max_seconds=%s resolved_lookback_minutes=%s",
        lb_days,
        max_sec,
        lookback_minutes,
    )

    async def ndjson_events():
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def worker() -> None:
            try:

                def send(ev: dict[str, Any]) -> None:
                    asyncio.run_coroutine_threadsafe(q.put(ev), loop).result(
                        timeout=None
                    )

                result = metamagi_label_voter_feedback_catchup(
                    lookback_minutes=lookback_minutes,
                    max_seconds=max_sec,
                    progress_callback=send,
                )
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "done", **result}), loop
                ).result(timeout=None)
            except Exception as exc:
                log.exception(
                    "HTTP /api/data/metamagi-label-catchup stream worker failed "
                    "(lookback_days=%s max_seconds=%s)",
                    lb_days,
                    max_sec,
                )
                asyncio.run_coroutine_threadsafe(
                    q.put({"type": "error", "detail": str(exc)}), loop
                ).result(timeout=None)
            finally:
                asyncio.run_coroutine_threadsafe(q.put(None), loop).result(
                    timeout=None
                )

        task = asyncio.create_task(asyncio.to_thread(worker))
        try:
            while True:
                item = await q.get()
                if item is None:
                    break
                yield json.dumps(item, default=str) + "\n"
        finally:
            await task

    return StreamingResponse(
        ndjson_events(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
        result = apply_execution_mode(body.execution_mode, body.confirmation_phrase)
        publish_bots_event("trading_settings", result)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class HaltBody(BaseModel):
    halted: bool


@app.put("/api/settings/trading/halt")
def put_trading_halt(body: HaltBody):
    set_global_halt(body.halted)
    snap = trading_settings_snapshot()
    publish_bots_event("trading_settings", snap)
    return snap


class RiskSettingsBody(BaseModel):
    base_risk_pct: float
    dynamic_tiers: list[dict[str, Any]]
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    consecutive_loss_limit: int
    enable_daily_loss_limit: bool = True
    enable_drawdown_protection: bool = True
    enable_consecutive_loss: bool = True
    enable_dynamic_sizing: bool = True
    enable_volatility_pause: bool = False
    volatility_threshold: float | None = None
    drawdown_action: str = "reduce"
    drawdown_reduce_factor: float = 0.5
    yolo_mode: bool = False


class RiskResetBody(BaseModel):
    source: str


class RiskYoloBody(BaseModel):
    yolo_mode: bool


@app.get("/api/settings/risk-defaults")
def get_risk_defaults():
    return {"risk_settings": get_global_risk_defaults()}


@app.put("/api/settings/risk-defaults")
def put_risk_defaults(body: RiskSettingsBody):
    try:
        settings = set_global_risk_defaults(body.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    publish_bots_event("risk_defaults_changed", {"risk_settings": settings})
    return {"risk_settings": settings}


@app.get("/api/bots/{bot_id}/risk-settings")
def get_bot_risk_settings_endpoint(bot_id: str):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM bots WHERE bot_id = ?", (bot_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Bot not found")
    finally:
        conn.close()
    row = get_bot_risk_settings(bot_id)
    return {
        "risk_settings": get_effective_bot_risk_settings(bot_id),
        "source": "bot" if row else "global",
    }


@app.put("/api/bots/{bot_id}/risk-settings")
def put_bot_risk_settings_endpoint(bot_id: str, body: RiskSettingsBody):
    try:
        settings = save_bot_risk_settings(bot_id, body.model_dump())
    except ValueError as e:
        msg = str(e)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from e
    publish_bot_event(
        bot_id,
        "risk_settings_updated",
        {"risk_settings": settings},
    )
    return {"risk_settings": settings}


@app.patch("/api/bots/{bot_id}/risk-settings/yolo")
def patch_bot_risk_yolo_endpoint(bot_id: str, body: RiskYoloBody):
    try:
        current = get_effective_bot_risk_settings(bot_id)
        settings = save_bot_risk_settings(
            bot_id,
            {**current, "yolo_mode": body.yolo_mode},
        )
    except ValueError as e:
        msg = str(e)
        code = 404 if "not found" in msg else 400
        raise HTTPException(status_code=code, detail=msg) from e
    publish_bot_event(bot_id, "risk_settings_updated", {"risk_settings": settings})
    return {"risk_settings": settings}


@app.post("/api/bots/{bot_id}/risk-settings/reset")
def reset_bot_risk_settings_endpoint(bot_id: str, body: RiskResetBody):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT strategy FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bot not found")
        strategy_name = str(row["strategy"] or "")
    finally:
        conn.close()

    if body.source == "global":
        target = get_global_risk_defaults()
    elif body.source == "template":
        target = template_risk_defaults(strategy_name)
    else:
        raise HTTPException(status_code=400, detail="source must be global or template")
    settings = save_bot_risk_settings(bot_id, target)
    publish_bot_event(bot_id, "risk_settings_updated", {"risk_settings": settings})
    return {"risk_settings": settings, "source": body.source}


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
            row["risk_settings"] = get_effective_bot_risk_settings(bot_id)
        return {"bots": rows}
    finally:
        conn.close()


class CreateBotBody(BaseModel):
    name: str
    symbol: str
    strategy: str = "sma_cross"  # defaults to original strategy for backward compat
    initial_budget_quote: float
    strategy_params: dict[str, Any] | None = None
    risk_settings: dict[str, Any] | None = None


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

    vv_raw = params.get("voters")
    if (
        isinstance(vv_raw, list)
        and vv_raw
        and all(isinstance(x, str) for x in vv_raw)
        and (
            body.strategy.startswith("magi_ensemble_")
            or body.strategy.startswith("magi_lag_ensemble_")
        )
    ):
        peer_w = peer_voter_weights_for_new_ensemble_bot(body.strategy, vv_raw)
        if peer_w:
            tmpl_w = params.get("voter_weights")
            tmpl_map: dict[str, Any] = tmpl_w if isinstance(tmpl_w, dict) else {}
            merged_vw: dict[str, float] = {}
            for v in vv_raw:
                if v in peer_w:
                    merged_vw[v] = peer_w[v]
                    continue
                raw_tw = tmpl_map.get(v, 1.0)
                try:
                    merged_vw[v] = round(float(raw_tw), 4)
                except (TypeError, ValueError):
                    merged_vw[v] = 1.0
            params["voter_weights"] = merged_vw
            logging.getLogger(__name__).info(
                "New ensemble bot: inherited voter_weights from peer (%s, %d weights)",
                body.strategy,
                len(peer_w),
            )

    params_json = json.dumps(params, default=str)
    try:
        bot = create_bot(body.name, body.symbol, body.strategy, params_json)
        risk_settings = (
            save_bot_risk_settings(bot["bot_id"], body.risk_settings)
            if body.risk_settings
            else save_bot_risk_settings(bot["bot_id"], get_global_risk_defaults())
        )
        bot["risk_settings"] = risk_settings
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    publish_bots_event("bots_changed", {"action": "created", "bot": bot})
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
    publish_bot_event(bot_id, "bot_updated", {"bot": bot})
    return {"bot": bot}


@app.delete("/api/bots/{bot_id}", status_code=204)
def delete_bot_endpoint(bot_id: str):
    try:
        delete_bot(bot_id)
    except ValueError as e:
        msg = str(e)
        code = 409 if "running" in msg else 404
        raise HTTPException(status_code=code, detail=msg) from e
    publish_bots_event("bots_changed", {"action": "deleted", "bot_id": bot_id})


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
    publish_bot_event(bot_id, "bot_updated", {"bot": bot})
    return {"bot": bot}


# TTL caches to rate-limit expensive exchange calls in GET /api/bots/{id}.
# Each bot gets one mark-price fetch and one order-sync per TTL window.
# This prevents the 4-second UI poll from hammering Binance and blocking
# the FastAPI thread pool while the bot runner is also active.
_mark_price_cache: dict[str, tuple[float, float]] = {}   # bot_id → (price, ts)
_order_sync_cache: dict[str, float] = {}                 # bot_id → last_sync_ts
_MARK_PRICE_TTL_SEC = 15.0
_ORDER_SYNC_TTL_SEC = 60.0

# Wallet balance is expensive (live Binance call). Cache per network view so
# rapid UI re-loads (page switches, polling) reuse the last known balance.
_wallet_cache: dict[str, tuple[dict, float]] = {}   # view → (payload, ts)
_WALLET_TTL_SEC = 30.0


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

        # Rate-limit the exchange order-sync: at most once per TTL window.
        now = time.time()
        if now - _order_sync_cache.get(bot_id, 0.0) >= _ORDER_SYNC_TTL_SEC:
            try:
                refresh_stale_bot_orders_from_exchange(bot_id, build_binance_spot(bot_mode))
                _order_sync_cache[bot_id] = now
            except Exception:
                pass

        order_stats, orders = fetch_bot_orders_panel(bot_id, order_limit=50)
        orders_asc = fetch_bot_orders_chronological(bot_id)

        # Rate-limit the mark-price fetch: reuse cached value within TTL.
        mark_price: float | None = None
        cached = _mark_price_cache.get(bot_id)
        if cached and now - cached[1] < _MARK_PRICE_TTL_SEC:
            mark_price = cached[0]
        elif sym:
            try:
                ex = build_binance_spot(bot_mode)
                if not ex.markets:
                    ex.load_markets()
                tk = ex.fetch_ticker(sym)
                lp = tk.get("last")
                if lp is not None:
                    mark_price = float(lp)
                    _mark_price_cache[bot_id] = (mark_price, now)
            except Exception:
                mark_price = cached[0] if cached else None
        perf = compute_strategy_performance(orders_asc, sym, mark_price=mark_price)
        total_pnl = perf["realized_pnl_quote"] + (perf["unrealized_pnl_quote"] or 0.0)
        raw_params = bot_row.get("strategy_params_json")
        bot_row["risk_settings"] = (
            db_row_to_risk_settings(get_bot_risk_settings(bot_id))
            or get_global_risk_defaults()
        )
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


@app.get("/api/bots/{bot_id}/trade-summary")
def get_bot_trade_summary(bot_id: str):
    """
    Return per-trade FIFO-matched closed trades for the bot.

    Each entry represents one sell event that consumed inventory, with:
      entry_price  — weighted average cost basis (FIFO)
      exit_price   — sell execution price
      realized_pnl — proceeds minus cost basis
      outcome      — 'win' | 'loss' | 'flat'
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT symbol, execution_mode FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Bot not found")
        sym = str(row["symbol"] or "")
        bot_mode = str(row["execution_mode"] or "testnet")
    finally:
        conn.close()

    orders_asc = fetch_bot_orders_chronological(bot_id)
    trades = compute_closed_trades(orders_asc, sym)
    return {
        "trades": trades,
        "symbol": sym,
        "execution_mode": bot_mode,
        "total_closed": len(trades),
    }


@app.get("/api/bots/{bot_id}/voter-signals")
def get_voter_signals(bot_id: str):
    """
    Return the latest signal cast by each voter for the given bot.
    Polled by the Bot Detail page to keep voter cards live.
    """
    rows = get_latest_voter_signals(bot_id)
    return {"voter_signals": rows}


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


@app.post("/api/bots/{bot_id}/optimize-weights")
def post_optimize_bot_weights(bot_id: str):
    """
    Run scripts/metamagi_labeled_export.py with blended voter weights, update bots.strategy_params_json,
    stream SSE logs + terminal-friendly script stderr lines.
    """
    return StreamingResponse(
        _optimize_weights_sse(bot_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
    reset_risk_protections: bool = False


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
        cur.execute(
            "SELECT bot_id, symbol, strategy_params_json FROM bots WHERE bot_id = ?",
            (bot_id,),
        )
        bot_row = cur.fetchone()
        if bot_row is None:
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

    risk_protections_reset = False
    if body.status == "running" and body.reset_risk_protections:
        ensure_bot_risk_settings(bot_id)
        raw_params = bot_row["strategy_params_json"]
        budget = initial_budget_from_strategy_params_json(
            raw_params if isinstance(raw_params, str) else None
        )
        orders = fetch_bot_orders_chronological(bot_id)
        state = risk_resume_state(
            orders_oldest_first=orders,
            symbol=str(bot_row["symbol"] or ""),
            initial_capital=float(budget or 1.0),
        )
        update_bot_risk_state(bot_id, state)
        risk_protections_reset = True

    payload = {
        "bot_id": bot_id,
        "status": body.status,
        "risk_protections_reset": risk_protections_reset,
    }
    publish_bot_event(bot_id, "bot_status", payload)
    return payload
