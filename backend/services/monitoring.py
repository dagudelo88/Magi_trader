"""Performance monitoring for MagiTrader.

Tracks DB operation timing, bot cycle duration, and WebSocket broadcast
latency.  Exposes a snapshot dict for /api/health and emits a one-line
health summary to the terminal every 60 seconds so hangs show up immediately
without having to watch for error messages.

Usage
-----
Import the module-level ``monitor`` singleton anywhere in the backend:

    from services.monitoring import monitor

    with monitor.timed_db("batch_insert_bot_logs"):
        ...

    monitor.record_bot_cycle(bot_id, elapsed_ms)
    monitor.maybe_log_health(running_bots=n, ws_clients=c)
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger("monitoring")

# ── Warning thresholds ─────────────────────────────────────────────────────

# DB: count slow ops above LOG threshold; emit WARNING above WARN threshold.
_SLOW_DB_LOG_MS: float = 200.0
_SLOW_DB_WARN_MS: float = 500.0

# Bot cycle: WARNING when a single bot's full cycle exceeds this.
_SLOW_CYCLE_MS: float = 500.0

# WS broadcast: WARNING when sending to one channel takes longer than this.
_SLOW_WS_MS: float = 100.0

# Health summary cadence.
_HEALTH_INTERVAL_SEC: float = 60.0


class PerformanceMonitor:
    """Thread-safe performance counter hub — one singleton per process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start = time.time()
        self._last_health_log: float = 0.0

        # ── DB ──────────────────────────────────────────────────────────────
        self.db_ops: int = 0
        self.db_slow_ops: int = 0        # ops > _SLOW_DB_LOG_MS
        self.db_total_ms: float = 0.0

        # ── Bot cycles ───────────────────────────────────────────────────────
        self.bot_cycles: int = 0
        self.bot_slow_cycles: int = 0
        self.bot_total_ms: float = 0.0
        self._bot_last_ms: dict[str, float] = {}

        # ── WS broadcasts ────────────────────────────────────────────────────
        self.ws_broadcasts: int = 0
        self.ws_slow_broadcasts: int = 0

        # ── Slow-op ring buffer (last 20 events) ─────────────────────────────
        self._slow_ops: deque[dict[str, Any]] = deque(maxlen=20)

        # ── Watchdog heartbeat ───────────────────────────────────────────────
        # Updated by maybe_log_health(); the watchdog background task checks
        # that this timestamp advances at least once per WATCHDOG_TIMEOUT_SEC.
        self._last_heartbeat: float = 0.0
        self._heartbeat_bots: int = 0
        self._heartbeat_clients: int = 0

    # ── DB timing ────────────────────────────────────────────────────────────

    def record_db_op(self, label: str, duration_ms: float) -> None:
        """Record a completed DB operation.  Thread-safe."""
        with self._lock:
            self.db_ops += 1
            self.db_total_ms += duration_ms
            if duration_ms > _SLOW_DB_LOG_MS:
                self.db_slow_ops += 1
                self._slow_ops.append({
                    "kind": "db",
                    "label": label,
                    "ms": round(duration_ms, 1),
                    "at": round(time.time()),
                })
        if duration_ms > _SLOW_DB_WARN_MS:
            logger.warning(
                "[SLOW DB] %s → %.0f ms  (warn threshold: %.0f ms)",
                label, duration_ms, _SLOW_DB_WARN_MS,
            )

    @contextmanager
    def timed_db(self, label: str):
        """Context manager: time a block and record it as a DB operation.

        Example::

            with monitor.timed_db("batch_insert_bot_logs"):
                conn.executemany(...)
                conn.commit()
        """
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record_db_op(label, (time.perf_counter() - t0) * 1000.0)

    # ── Bot cycle timing ─────────────────────────────────────────────────────

    def record_bot_cycle(self, bot_id: str, duration_ms: float) -> None:
        """Record the wall-clock duration for one bot's complete processing cycle."""
        with self._lock:
            self.bot_cycles += 1
            self.bot_total_ms += duration_ms
            self._bot_last_ms[bot_id] = round(duration_ms, 1)
            if duration_ms > _SLOW_CYCLE_MS:
                self.bot_slow_cycles += 1
                self._slow_ops.append({
                    "kind": "bot_cycle",
                    "bot_id": bot_id,
                    "ms": round(duration_ms, 1),
                    "at": round(time.time()),
                })
        if duration_ms > _SLOW_CYCLE_MS:
            logger.warning(
                "[SLOW BOT] bot=%s → %.0f ms  (warn threshold: %.0f ms)",
                bot_id, duration_ms, _SLOW_CYCLE_MS,
            )

    # ── WS broadcast timing ──────────────────────────────────────────────────

    def record_ws_broadcast(
        self, channel: str, n_clients: int, duration_ms: float
    ) -> None:
        """Record a completed WebSocket broadcast."""
        with self._lock:
            self.ws_broadcasts += 1
            if duration_ms > _SLOW_WS_MS:
                self.ws_slow_broadcasts += 1
                self._slow_ops.append({
                    "kind": "ws",
                    "channel": channel,
                    "clients": n_clients,
                    "ms": round(duration_ms, 1),
                    "at": round(time.time()),
                })
        if duration_ms > _SLOW_WS_MS:
            logger.warning(
                "[SLOW WS] channel=%s  clients=%d → %.0f ms",
                channel, n_clients, duration_ms,
            )

    # ── Health snapshot & logging ────────────────────────────────────────────

    def snapshot(self, running_bots: int = 0, ws_clients: int = 0) -> dict[str, Any]:
        """Return a JSON-serialisable health dict.  Thread-safe."""
        with self._lock:
            avg_db = (self.db_total_ms / self.db_ops) if self.db_ops else 0.0
            avg_cycle = (self.bot_total_ms / self.bot_cycles) if self.bot_cycles else 0.0
            return {
                "uptime_sec": round(time.time() - self._start),
                "running_bots": running_bots,
                "ws_clients": ws_clients,
                "db": {
                    "ops_total": self.db_ops,
                    "slow_ops": self.db_slow_ops,
                    "avg_ms": round(avg_db, 1),
                },
                "bots": {
                    "cycles_total": self.bot_cycles,
                    "slow_cycles": self.bot_slow_cycles,
                    "avg_cycle_ms": round(avg_cycle, 1),
                    "last_cycle_ms": dict(self._bot_last_ms),
                },
                "ws": {
                    "broadcasts_total": self.ws_broadcasts,
                    "slow_broadcasts": self.ws_slow_broadcasts,
                },
                "recent_slow_ops": list(self._slow_ops)[-10:],
            }

    def watchdog_status(self) -> dict[str, Any]:
        """Return watchdog state: seconds elapsed since the last health heartbeat.

        The watchdog background task calls this to detect silent hangs.  A
        ``seconds_since_update`` value greater than WATCHDOG_TIMEOUT_SEC means
        the application is likely stuck.
        """
        with self._lock:
            if self._last_heartbeat == 0.0:
                return {
                    "seconds_since_update": None,
                    "healthy": False,
                    "last_bots": 0,
                    "last_clients": 0,
                }
            elapsed = time.monotonic() - self._last_heartbeat
            return {
                "seconds_since_update": round(elapsed),
                "healthy": True,
                "last_bots": self._heartbeat_bots,
                "last_clients": self._heartbeat_clients,
            }

    def maybe_log_health(self, running_bots: int = 0, ws_clients: int = 0) -> None:
        """Emit a one-line health summary at most once per _HEALTH_INTERVAL_SEC.

        Safe to call from any thread or coroutine — the 60-second gate is
        enforced under a lock so concurrent callers never double-log.
        Also updates the watchdog heartbeat so the background watchdog task
        can confirm the application is still making progress.
        """
        now = time.monotonic()
        with self._lock:
            if now - self._last_health_log < _HEALTH_INTERVAL_SEC:
                return
            self._last_health_log = now
            # Update watchdog heartbeat atomically with the health log.
            self._last_heartbeat = now
            self._heartbeat_bots = running_bots
            self._heartbeat_clients = ws_clients
            # Capture values atomically while holding the lock.
            db_ops = self.db_ops
            db_slow = self.db_slow_ops
            avg_db = (self.db_total_ms / self.db_ops) if self.db_ops else 0.0
            avg_cycle = (self.bot_total_ms / self.bot_cycles) if self.bot_cycles else 0.0
            slow_total = self.db_slow_ops + self.bot_slow_cycles + self.ws_slow_broadcasts
            ws_bc = self.ws_broadcasts
        # Log outside the lock so logger handlers can't deadlock us.
        logger.info(
            "Health: %d bots | %d WS clients | DB ops: %d (slow: %d) | "
            "Avg DB: %.0fms | Avg cycle: %.0fms | WS broadcasts: %d | Total slow: %d",
            running_bots, ws_clients, db_ops, db_slow,
            avg_db, avg_cycle, ws_bc, slow_total,
        )


# Module-level singleton — import this in any module that records timings.
monitor = PerformanceMonitor()
