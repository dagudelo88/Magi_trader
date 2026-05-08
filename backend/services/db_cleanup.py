"""
Smart retention + archive for MagiTrader's SQLite database.

Strategy
--------
High-volume live tables are pruned by moving old rows into *_archive mirror
tables instead of deleting them.  100 % of historical data is preserved for
future ML training while the hot live tables stay small and fast.

Default retention windows (overridable via kwargs / env / CLI flags):
    voter_feedback   → 14 days in live  (ML training feature store)
    bot_decisions    → 14 days in live
    bot_logs         → 7  days in live
    market_ticks     → 3  days in live  (grows fastest — ~86 k rows/day at 1 Hz)

Usage
-----
  # Run from the repo root (or backend/ folder):
  python backend/services/db_cleanup.py

  # Custom retention:
  python backend/services/db_cleanup.py --voter-days 30 --tick-days 7

  # Skip VACUUM (faster, useful for scheduled mid-day runs):
  python backend/services/db_cleanup.py --no-vacuum

Called programmatically from main.py once per day via _db_cleanup_loop().
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Locate the DB relative to this file's position in the repo tree.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_DIR = os.path.dirname(_THIS_DIR)
DB_PATH = os.path.join(_BACKEND_DIR, "..", "data", "magitrader.db")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

class CleanupResult(NamedTuple):
    voter_feedback_moved: int
    bot_decisions_moved: int
    bot_logs_moved: int
    market_ticks_moved: int
    vacuumed: bool


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    """Open a WAL-mode connection with a generous timeout for the cleanup workload."""
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _move_rows(
    conn: sqlite3.Connection,
    src_table: str,
    dst_table: str,
    ts_column: str,
    pk_column: str,
    cutoff_ms: int,
    batch_size: int = 5_000,
) -> int:
    """
    Move rows older than *cutoff_ms* from *src_table* → *dst_table* in batches.

    Uses INSERT OR IGNORE so rerunning after a partial failure is safe (already
    archived rows are silently skipped, the DELETE just finds nothing to remove).
    Returns the total number of rows moved.
    """
    cur = conn.cursor()
    total = 0

    while True:
        cur.execute(
            # noqa: S608 – table and column names are hard-coded constants
            f"SELECT {pk_column} FROM {src_table} WHERE {ts_column} < ? LIMIT ?",
            (cutoff_ms, batch_size),
        )
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            break

        placeholders = ",".join("?" * len(ids))

        cur.execute(
            f"INSERT OR IGNORE INTO {dst_table}"              # noqa: S608
            f"  SELECT * FROM {src_table}"
            f"  WHERE {pk_column} IN ({placeholders})",
            ids,
        )
        cur.execute(
            f"DELETE FROM {src_table} WHERE {pk_column} IN ({placeholders})",  # noqa: S608
            ids,
        )
        conn.commit()
        total += len(ids)
        logger.debug("  archived %d rows from %s (running total %d)", len(ids), src_table, total)

    return total


def _move_bot_decisions(
    conn: sqlite3.Connection,
    cutoff_ms: int,
    batch_size: int = 5_000,
) -> int:
    """
    Archive bot_decisions rows that are old enough.

    Priority order (both paths are applied):
    1. Rows with created_at set (new format written by batch_record_bot_decisions).
    2. Rows with tick_id pointing at an already-archived market_tick (legacy rows
       that pre-date the created_at column migration).
    """
    cur = conn.cursor()
    total = 0

    # -- Path 1: rows with created_at column populated ----------------------
    while True:
        cur.execute(
            "SELECT decision_id FROM bot_decisions"
            " WHERE created_at IS NOT NULL AND created_at < ? LIMIT ?",
            (cutoff_ms, batch_size),
        )
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            break
        ph = ",".join("?" * len(ids))
        cur.execute(
            f"INSERT OR IGNORE INTO bot_decisions_archive"  # noqa: S608
            f"  SELECT * FROM bot_decisions WHERE decision_id IN ({ph})",
            ids,
        )
        cur.execute(
            f"DELETE FROM bot_decisions WHERE decision_id IN ({ph})",  # noqa: S608
            ids,
        )
        conn.commit()
        total += len(ids)

    # -- Path 2: legacy rows with NULL created_at but archived tick_id ------
    # Find the max tick_id that has already been moved to the archive table at
    # or before the cutoff.  This is set after market_ticks archival completes.
    cur.execute(
        "SELECT MAX(tick_id) FROM market_ticks_archive WHERE timestamp < ?",
        (cutoff_ms,),
    )
    row = cur.fetchone()
    max_archived_tick: int | None = row[0] if row and row[0] is not None else None

    if max_archived_tick is not None:
        while True:
            cur.execute(
                "SELECT decision_id FROM bot_decisions"
                " WHERE created_at IS NULL"
                "   AND tick_id IS NOT NULL"
                "   AND tick_id <= ? LIMIT ?",
                (max_archived_tick, batch_size),
            )
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                break
            ph = ",".join("?" * len(ids))
            cur.execute(
                f"INSERT OR IGNORE INTO bot_decisions_archive"  # noqa: S608
                f"  SELECT * FROM bot_decisions WHERE decision_id IN ({ph})",
                ids,
            )
            cur.execute(
                f"DELETE FROM bot_decisions WHERE decision_id IN ({ph})",  # noqa: S608
                ids,
            )
            conn.commit()
            total += len(ids)

    return total


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_cleanup(
    voter_days: int = 14,
    decisions_days: int = 14,
    logs_days: int = 7,
    tick_days: int = 3,
    vacuum: bool = True,
) -> CleanupResult:
    """
    Move old rows from live tables → archive tables, then optionally VACUUM.

    Safe to call repeatedly — INSERT OR IGNORE makes the archival step fully
    idempotent.  All data is preserved in *_archive tables; nothing is deleted
    from the overall database.

    Parameters
    ----------
    voter_days      Days to keep in live voter_feedback  (default 14)
    decisions_days  Days to keep in live bot_decisions   (default 14)
    logs_days       Days to keep in live bot_logs        (default  7)
    tick_days       Days to keep in live market_ticks    (default  3)
    vacuum          Run VACUUM after archival if rows were moved (default True)
    """
    if not os.path.exists(DB_PATH):
        logger.warning("DB not found at %s — skipping cleanup.", DB_PATH)
        return CleanupResult(0, 0, 0, 0, False)

    now_ms = int(time.time() * 1000)
    ms_per_day = 86_400_000

    voter_cutoff = now_ms - voter_days * ms_per_day
    decisions_cutoff = now_ms - decisions_days * ms_per_day
    logs_cutoff = now_ms - logs_days * ms_per_day
    tick_cutoff = now_ms - tick_days * ms_per_day

    conn = _get_conn()
    try:
        # Archive tables are created by init_db() on startup; this is a safety net
        # for the case where cleanup is called before the server starts (e.g. CLI).
        from database import _create_archive_tables  # type: ignore[import]
        _create_archive_tables(conn.cursor())
        conn.commit()
    except Exception:
        pass  # non-fatal — tables likely already exist

    # Apply the bot_decisions.created_at migration if the server hasn't started
    # yet (init_db() normally does this, but the CLI may run first).
    try:
        conn.execute("ALTER TABLE bot_decisions ADD COLUMN created_at INTEGER")
        conn.commit()
    except Exception:
        pass  # column already exists — safe to ignore

    vf_moved = bd_moved = bl_moved = mt_moved = 0

    try:
        logger.info("DB cleanup: archiving voter_feedback older than %d days…", voter_days)
        vf_moved = _move_rows(
            conn, "voter_feedback", "voter_feedback_archive",
            "timestamp", "feedback_id", voter_cutoff,
        )

        logger.info("DB cleanup: archiving bot_logs older than %d days…", logs_days)
        bl_moved = _move_rows(
            conn, "bot_logs", "bot_logs_archive",
            "created_at", "log_id", logs_cutoff,
        )

        # Archive market_ticks BEFORE bot_decisions so that _move_bot_decisions
        # can correlate legacy rows via max(tick_id) in market_ticks_archive.
        logger.info("DB cleanup: archiving market_ticks older than %d days…", tick_days)
        mt_moved = _move_rows(
            conn, "market_ticks", "market_ticks_archive",
            "timestamp", "tick_id", tick_cutoff,
        )

        logger.info("DB cleanup: archiving bot_decisions older than %d days…", decisions_days)
        bd_moved = _move_bot_decisions(conn, decisions_cutoff)

        logger.info(
            "DB cleanup complete — voter_feedback=%d  bot_decisions=%d  "
            "bot_logs=%d  market_ticks=%d",
            vf_moved, bd_moved, bl_moved, mt_moved,
        )
    except Exception:
        logger.exception("DB cleanup failed during archival — partial results may apply.")
    finally:
        conn.close()

    did_vacuum = False
    total_moved = vf_moved + bd_moved + bl_moved + mt_moved
    if vacuum and total_moved > 0:
        # VACUUM requires no active transaction and no other writers; open a
        # fresh autocommit connection so SQLite accepts the VACUUM command.
        try:
            logger.info("DB cleanup: running VACUUM (%d rows freed)…", total_moved)
            vconn = sqlite3.connect(DB_PATH, timeout=120, isolation_level=None)
            try:
                vconn.execute("VACUUM")
            finally:
                vconn.close()
            logger.info("DB cleanup: VACUUM complete.")
            did_vacuum = True
        except Exception:
            logger.warning("VACUUM failed (non-fatal — data is intact).", exc_info=True)

    return CleanupResult(
        voter_feedback_moved=vf_moved,
        bot_decisions_moved=bd_moved,
        bot_logs_moved=bl_moved,
        market_ticks_moved=mt_moved,
        vacuumed=did_vacuum,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args():
    import argparse

    parser = argparse.ArgumentParser(
        description="Archive old MagiTrader DB rows to *_archive tables.",
    )
    parser.add_argument("--voter-days",     type=int, default=14,
                        help="Days to keep in live voter_feedback (default 14)")
    parser.add_argument("--decisions-days", type=int, default=14,
                        help="Days to keep in live bot_decisions (default 14)")
    parser.add_argument("--logs-days",      type=int, default=7,
                        help="Days to keep in live bot_logs (default 7)")
    parser.add_argument("--tick-days",      type=int, default=3,
                        help="Days to keep in live market_ticks (default 3)")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="Skip VACUUM after archival")
    return parser.parse_args()


if __name__ == "__main__":
    # Make sure backend/ is on the path so `from database import ...` works.
    _backend = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _backend not in sys.path:
        sys.path.insert(0, _backend)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        stream=sys.stdout,
    )

    args = _parse_args()
    result = run_cleanup(
        voter_days=args.voter_days,
        decisions_days=args.decisions_days,
        logs_days=args.logs_days,
        tick_days=args.tick_days,
        vacuum=not args.no_vacuum,
    )
    print(
        f"\nSummary:\n"
        f"  voter_feedback  moved: {result.voter_feedback_moved}\n"
        f"  bot_decisions   moved: {result.bot_decisions_moved}\n"
        f"  bot_logs        moved: {result.bot_logs_moved}\n"
        f"  market_ticks    moved: {result.market_ticks_moved}\n"
        f"  VACUUM run:            {result.vacuumed}\n"
    )
