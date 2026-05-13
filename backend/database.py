import json
import logging
import queue
import re
import secrets
import sqlite3
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable

# Log lines emitted by bot_runner (legacy + current) — used to backfill bot_orders.
_RE_BUY_FROM_LOG = re.compile(
    r"BUY (?:market order placed|filled/accepted) id=(\S+)\s+quoteOrderQty=([\d.eE+-]+)"
    r"(?:\s+status=(\S+))?",
)
_RE_SELL_FROM_LOG = re.compile(
    r"SELL (?:market order placed|filled/accepted) id=(\S+)\s+amount=([\d.eE+-]+)"
    r"(?:\s+status=(\S+))?",
)

_last_exchange_refresh_mono: dict[str, float] = {}

_metamagi_catchup_log = logging.getLogger("metamagi_catchup")


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _derive_execution_average(fo: dict[str, Any]) -> float | None:
    avg = _f(fo.get("average"))
    if avg is not None:
        return avg
    filled = _f(fo.get("filled"))
    cost = _f(fo.get("cost"))
    if filled is not None and cost is not None and filled > 0:
        return cost / filled
    info = fo.get("info") or {}
    for key in ("avgPrice", "avg_price", "price"):
        v = info.get(key)
        a = _f(v)
        if a is not None and a > 0:
            return a
    exq = _f(info.get("executedQty") or info.get("executed_qty"))
    cq = _f(info.get("cummulativeQuoteQty") or info.get("cummulative_quote_qty"))
    if exq is not None and cq is not None and exq > 0:
        return cq / exq
    return None


def _display_price_for_order(row: dict[str, Any], raw_str: str | None) -> float | None:
    avg = _f(row.get("average"))
    if avg is not None:
        return avg
    filled = _f(row.get("filled"))
    cost = _f(row.get("cost"))
    if filled is not None and cost is not None and filled > 0:
        return cost / filled
    if not raw_str:
        return None
    try:
        raw = json.loads(raw_str)
        return _derive_execution_average(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _display_status_for_order(row: dict[str, Any], raw_str: str | None) -> str:
    s = (row.get("status") or "").strip()
    if s and s.lower() != "unknown":
        return s.upper()
    if raw_str:
        try:
            raw = json.loads(raw_str)
            st = raw.get("status")
            if st:
                return str(st).upper()
            info = raw.get("info") or {}
            if isinstance(info, dict) and info.get("status"):
                return str(info["status"]).upper()
        except json.JSONDecodeError:
            pass
    return "FILLED"


def refresh_stale_bot_orders_from_exchange(bot_id: str, ex: Any, cooldown_sec: float = 45.0) -> None:
    """
    For rows missing avg/status, pull the latest order snapshot from the exchange (CCXT).
    Throttled per bot_id to avoid rate limits when the UI polls.
    """
    now = time.monotonic()
    if now - _last_exchange_refresh_mono.get(bot_id, 0) < cooldown_sec:
        return

    conn = get_db_connection()
    touched = False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT order_row_id, exchange_order_id, symbol
            FROM bot_orders
            WHERE bot_id = ?
              AND exchange_order_id IS NOT NULL
              AND (
                    average IS NULL
                 OR TRIM(COALESCE(status, '')) = ''
                 OR LOWER(TRIM(status)) = 'unknown'
              )
            ORDER BY created_at DESC
            LIMIT 12
            """,
            (bot_id,),
        )
        rows = cur.fetchall()
        if not rows:
            _last_exchange_refresh_mono[bot_id] = now
            return

        if not getattr(ex, "markets", None):
            ex.load_markets()

        for r in rows:
            try:
                fo = ex.fetch_order(str(r["exchange_order_id"]), r["symbol"])
            except Exception:
                continue
            avg = _derive_execution_average(fo)
            st = str(fo.get("status") or "").strip().upper() or "FILLED"
            raw = json.dumps(fo, default=str)
            if len(raw) > 32000:
                raw = raw[:32000] + "…"
            cur.execute(
                """
                UPDATE bot_orders
                SET average = COALESCE(?, average),
                    filled = COALESCE(?, filled),
                    cost = COALESCE(?, cost),
                    amount = COALESCE(?, amount),
                    status = ?,
                    raw_response_json = ?
                WHERE order_row_id = ?
                """,
                (
                    avg,
                    fo.get("filled"),
                    fo.get("cost"),
                    fo.get("amount"),
                    st,
                    raw,
                    r["order_row_id"],
                ),
            )
            touched = True
        if touched:
            conn.commit()
        _last_exchange_refresh_mono[bot_id] = now
    finally:
        conn.close()


DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")

# ── Connection pool configuration ──────────────────────────────────────────
# Reduces open/close overhead: instead of creating a fresh OS-level file
# descriptor for every query, callers borrow a pre-warmed connection and
# return it to the pool via close().

DB_POOL_SIZE: int = int(os.environ.get("DB_POOL_SIZE", "10"))

_pool_logger = __import__("logging").getLogger("db_pool")


class _PooledConnection:
    """Proxy for a sqlite3.Connection that returns it to the pool on close().

    All attribute access (execute, executemany, commit, cursor, row_factory …)
    is forwarded to the real connection so callers need no code changes.
    """

    __slots__ = ("_conn", "_pool")

    def __init__(self, conn: sqlite3.Connection, pool: "_ConnectionPool") -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("_conn", "_pool"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def close(self) -> None:
        pool = object.__getattribute__(self, "_pool")
        conn = object.__getattribute__(self, "_conn")
        pool._release(conn)


class _ConnectionPool:
    """Thread-safe SQLite connection pool.

    Connections are created with check_same_thread=False because each
    connection is held by at most one thread at a time (enforced by the
    internal queue).  WAL journal mode (set once by _enable_wal_once) allows
    multiple readers to coexist with a single writer so the pool does not
    create write-contention beyond what SQLite already handles.

    On acquire():
      - If an idle connection is available it is returned immediately (pool hit).
      - If the queue is exhausted an overflow connection is created and logged
        as a pool miss — this should be rare; if it becomes frequent, raise
        DB_POOL_SIZE.

    On close() of the returned _PooledConnection the underlying connection is
    put back into the queue.  If the queue is already full (overflow case) the
    connection is truly closed.
    """

    def __init__(self, size: int) -> None:
        self._size = size
        self._q: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=size)
        self._lock = threading.Lock()
        self._created: int = 0
        self._acquired: int = 0
        self._active: int = 0
        self._pool_hits: int = 0
        self._pool_misses: int = 0

        for _ in range(size):
            self._q.put(self._make_conn())

    def _make_conn(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=10000")
        conn.execute("PRAGMA temp_store=MEMORY")
        with self._lock:
            self._created += 1
            n = self._created
        _pool_logger.debug("DB pool: created connection #%d (pool_size=%d)", n, self._size)
        return conn

    def acquire(self) -> _PooledConnection:
        with self._lock:
            self._acquired += 1
            self._active += 1
        try:
            conn = self._q.get(timeout=15)
            with self._lock:
                self._pool_hits += 1
        except queue.Empty:
            with self._lock:
                self._pool_misses += 1
            _pool_logger.warning(
                "DB pool exhausted (size=%d) — creating overflow connection "
                "(consider raising DB_POOL_SIZE)",
                self._size,
            )
            conn = self._make_conn()
        return _PooledConnection(conn, self)

    def _release(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._active -= 1
        try:
            self._q.put_nowait(conn)
        except queue.Full:
            conn.close()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "pool_size": self._size,
                "idle": self._q.qsize(),
                "active": self._active,
                "total_created": self._created,
                "total_acquired": self._acquired,
                "pool_hits": self._pool_hits,
                "pool_misses": self._pool_misses,
            }


# Lazily initialized singleton — created on first get_db_connection() call.
_pool: _ConnectionPool | None = None
_pool_init_lock = threading.Lock()


def _get_pool() -> _ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_init_lock:
            if _pool is None:
                _pool = _ConnectionPool(DB_POOL_SIZE)
    return _pool


def get_pool_stats() -> dict[str, Any]:
    """Return current connection pool statistics (safe before pool is created)."""
    global _pool
    if _pool is None:
        return {"pool_size": DB_POOL_SIZE, "status": "not_initialized"}
    return _pool.stats()


def _report_db_timing(label: str, duration_ms: float) -> None:
    """Forward a completed DB operation's timing to the performance monitor.

    Uses a lazy import so database.py stays importable even before the services
    package is on sys.path (e.g. standalone __main__ runs).  Never raises.
    """
    try:
        from services.monitoring import monitor  # noqa: PLC0415
        monitor.record_db_op(label, duration_ms)
    except Exception:
        pass


def get_db_connection() -> _PooledConnection:
    """Borrow a SQLite connection from the thread-safe connection pool.

    Returns a _PooledConnection proxy; call .close() when done (the existing
    try/finally pattern is unchanged).  The underlying connection is returned
    to the pool rather than being truly closed, eliminating the per-operation
    file-open/close overhead.

    Pool connections are pre-warmed with the same performance pragmas that were
    previously applied on each fresh connection:
      * synchronous=NORMAL  — safe with WAL; ~3× faster than FULL
      * cache_size=10000    — 40 MB page cache (vs SQLite default of 2 MB)
      * temp_store=MEMORY   — keep temp tables/indexes in RAM, not disk

    Pool size is controlled by the DB_POOL_SIZE env var (default: 10).
    """
    return _get_pool().acquire()


def get_direct_db_connection(timeout_ms: int = 15_000) -> sqlite3.Connection:
    """Open a non-pooled connection for low-priority background jobs.

    MetaMagi uses this with a very short busy timeout so it yields instead of
    blocking runtime writers for the pool's normal 15 second timeout.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH,
        timeout=max(0.001, timeout_ms / 1000.0),
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=10000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout_ms))}")
    return conn

def _enable_wal_once() -> None:
    """
    Switch the database to WAL journal mode the first time it is initialised.

    WAL mode is persistent on the file — this only needs to succeed once.
    A failed attempt (e.g. stale lock on first boot) is silently ignored;
    the next restart will retry.  Never called from get_db_connection() so
    routine connections carry zero overhead and cannot trigger a startup crash.
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=3)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.close()
        if mode != "wal":
            import logging
            logging.getLogger(__name__).warning(
                "journal_mode=%s (WAL not active — stale lock?)", mode
            )
    except Exception:
        pass  # Non-fatal: delete-mode still works; WAL will be set on next restart.


def _create_archive_tables(cursor: sqlite3.Cursor) -> None:
    """
    Create _archive mirror tables for the four high-volume live tables.
    All archive tables use INSERT OR IGNORE so the cleanup script is idempotent.
    No FOREIGN KEY constraints — archived rows must survive live-table purges.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voter_feedback_archive (
            feedback_id       INTEGER PRIMARY KEY,
            bot_id            TEXT,
            timestamp         INTEGER NOT NULL,
            target_asset      TEXT    NOT NULL,
            ensemble_signal   TEXT    NOT NULL,
            voter_name        TEXT    NOT NULL,
            voter_signal      TEXT    NOT NULL,
            confidence        REAL,
            forward_roc_30s   REAL,
            forward_roc_5m    REAL,
            realized_pnl      REAL,
            consensus_score   REAL,
            features_snapshot TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vfa_asset_ts "
        "ON voter_feedback_archive(target_asset, timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vfa_bot_ts "
        "ON voter_feedback_archive(bot_id, timestamp)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_decisions_archive (
            decision_id INTEGER PRIMARY KEY,
            bot_id      TEXT,
            tick_id     INTEGER,
            mode        TEXT,
            action      TEXT,
            confidence  REAL,
            executed    BOOLEAN,
            created_at  INTEGER
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bda_bot_id "
        "ON bot_decisions_archive(bot_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bda_created_at "
        "ON bot_decisions_archive(created_at)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs_archive (
            log_id         INTEGER PRIMARY KEY,
            bot_id         TEXT    NOT NULL,
            created_at     INTEGER NOT NULL,
            level          TEXT    NOT NULL,
            execution_mode TEXT    NOT NULL,
            message        TEXT    NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bla_bot_time "
        "ON bot_logs_archive(bot_id, created_at DESC)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_ticks_archive (
            tick_id             INTEGER PRIMARY KEY,
            timestamp           INTEGER NOT NULL,
            target_asset        TEXT    NOT NULL,
            target_price        REAL,
            btc_price           REAL,
            btc_roc_1s          REAL,
            btc_roc_5s          REAL,
            target_roc_1s       REAL,
            target_roc_5s       REAL,
            btc_volume_delta    REAL,
            target_volume_delta REAL,
            spread_bps          REAL,
            features_json       TEXT
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_mta_timestamp "
        "ON market_ticks_archive(timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_mta_asset "
        "ON market_ticks_archive(target_asset)"
    )


def init_db():
    import logging as _logging
    _log = _logging.getLogger(__name__)
    _log.info("=== MagiTrader - DB Pooling + Watchdog Mode ACTIVE ===")
    _log.info(
        "DB Pooling ENABLED — pool_size=%d, WAL journal, synchronous=NORMAL, "
        "cache_size=10000, temp_store=MEMORY, timeout=15s",
        DB_POOL_SIZE,
    )
    _get_pool()  # pre-warm the pool before any DB access
    _enable_wal_once()
    conn = get_db_connection()
    cursor = conn.cursor()

    # Core table for ML Lead-Lag Arbitrage ticks
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_ticks (
            tick_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp INTEGER NOT NULL,
            target_asset TEXT NOT NULL,
            target_price REAL,
            btc_price REAL,
            btc_roc_1s REAL,
            btc_roc_5s REAL,
            target_roc_1s REAL,
            target_roc_5s REAL,
            btc_volume_delta REAL,
            target_volume_delta REAL,
            spread_bps REAL,
            features_json TEXT
        )
    """)
    
    # Indexes for faster time-series queries
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_market_ticks_timestamp ON market_ticks(timestamp)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_market_ticks_asset ON market_ticks(target_asset)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_ticks_asset_ts "
        "ON market_ticks(target_asset, timestamp)"
    )

    # Order book snapshots
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS market_depth (
            depth_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tick_id INTEGER,
            symbol TEXT NOT NULL,
            bids_json TEXT,
            asks_json TEXT,
            FOREIGN KEY (tick_id) REFERENCES market_ticks(tick_id)
        )
    """)

    # Bot decisions logged alongside ticks for later ML labeling/training
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT,
            tick_id INTEGER,
            mode TEXT,
            action TEXT,
            confidence REAL,
            executed BOOLEAN,
            FOREIGN KEY (tick_id) REFERENCES market_ticks(tick_id)
        )
    """)

    # Runtime app settings (trading mode, killswitch) — not secrets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # User-configurable bots
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            bot_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'sma_cross',
            strategy_params_json TEXT,
            status TEXT NOT NULL DEFAULT 'stopped',
            execution_mode TEXT NOT NULL DEFAULT 'testnet',
            capital_source TEXT NOT NULL DEFAULT 'budget',
            live_initial_capital_quote REAL,
            testnet_initial_capital_quote REAL,
            created_at INTEGER NOT NULL
        )
    """)
    # Migration: add execution_mode column to existing databases
    try:
        cursor.execute("ALTER TABLE bots ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'testnet'")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Migration: track when a bot was last set to running
    try:
        cursor.execute("ALTER TABLE bots ADD COLUMN started_at INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    for col, col_def in [
        ("capital_source", "TEXT NOT NULL DEFAULT 'budget'"),
        ("live_initial_capital_quote", "REAL"),
        ("testnet_initial_capital_quote", "REAL"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE bots ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # Backfill the new per-mode testnet initial capital from legacy strategy params.
    try:
        cursor.execute(
            """
            SELECT bot_id, strategy_params_json
            FROM bots
            WHERE testnet_initial_capital_quote IS NULL
            """
        )
        backfills: list[tuple[float, str]] = []
        for row in cursor.fetchall():
            raw = row["strategy_params_json"]
            budget: float | None = None
            if isinstance(raw, str) and raw.strip():
                try:
                    params = json.loads(raw)
                except json.JSONDecodeError:
                    params = {}
                if isinstance(params, dict):
                    for key in ("initial_budget_quote", "trading_budget_quote", "budget_usdt"):
                        if key not in params:
                            continue
                        try:
                            value = float(params[key])
                        except (TypeError, ValueError):
                            continue
                        if value > 0:
                            budget = value
                            break
            if budget is not None:
                backfills.append((budget, row["bot_id"]))
        if backfills:
            cursor.executemany(
                "UPDATE bots SET testnet_initial_capital_quote = ? WHERE bot_id = ?",
                backfills,
            )
    except Exception:
        pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            level TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            message TEXT NOT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bot_logs_bot_time ON bot_logs(bot_id, created_at DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_orders (
            order_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            exchange_order_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            amount REAL,
            cost REAL,
            average REAL,
            filled REAL,
            status TEXT,
            raw_response_json TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_orders_bot_time ON bot_orders(bot_id, created_at DESC)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_orders_bot_mode_time "
        "ON bot_orders(bot_id, execution_mode, created_at DESC)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_capital_flows (
            flow_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            amount_quote REAL NOT NULL,
            flow_type TEXT NOT NULL,
            reason TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_capital_flows_bot_mode_time "
        "ON bot_capital_flows(bot_id, execution_mode, created_at DESC)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_risk_settings (
            bot_id TEXT PRIMARY KEY,
            base_risk_pct REAL NOT NULL,
            dynamic_tiers_json TEXT NOT NULL,
            daily_loss_limit_pct REAL NOT NULL,
            max_drawdown_pct REAL NOT NULL,
            consecutive_loss_limit INTEGER NOT NULL,
            enable_daily_loss_limit INTEGER NOT NULL DEFAULT 1,
            enable_drawdown_protection INTEGER NOT NULL DEFAULT 1,
            enable_consecutive_loss INTEGER NOT NULL DEFAULT 1,
            enable_dynamic_sizing INTEGER NOT NULL DEFAULT 1,
            enable_volatility_pause INTEGER NOT NULL DEFAULT 0,
            volatility_threshold REAL,
            drawdown_action TEXT NOT NULL DEFAULT 'reduce',
            drawdown_reduce_factor REAL NOT NULL DEFAULT 0.5,
            yolo_mode INTEGER NOT NULL DEFAULT 0,
            consecutive_loss_baseline INTEGER NOT NULL DEFAULT 0,
            daily_loss_baseline_date TEXT,
            daily_loss_baseline_pnl REAL NOT NULL DEFAULT 0,
            drawdown_baseline_pct REAL NOT NULL DEFAULT 0,
            last_risk_pause_reason TEXT,
            last_manual_resume_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )
    """)
    risk_state_migrations = [
        ("yolo_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("consecutive_loss_baseline", "INTEGER NOT NULL DEFAULT 0"),
        ("daily_loss_baseline_date", "TEXT"),
        ("daily_loss_baseline_pnl", "REAL NOT NULL DEFAULT 0"),
        ("drawdown_baseline_pct", "REAL NOT NULL DEFAULT 0"),
        ("last_risk_pause_reason", "TEXT"),
        ("last_manual_resume_at", "INTEGER"),
    ]
    for col, col_def in risk_state_migrations:
        try:
            cursor.execute(
                f"ALTER TABLE bot_risk_settings ADD COLUMN {col} {col_def}"
            )
        except sqlite3.OperationalError:
            pass

    # Migration: add created_at to bot_decisions so rows can be time-bounded for
    # archival and ML training queries.  Old rows will have NULL; new rows are
    # stamped by batch_record_bot_decisions().
    try:
        cursor.execute("ALTER TABLE bot_decisions ADD COLUMN created_at INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists
    # Indexes for common ML training query patterns on bot_decisions.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_decisions_bot_id "
        "ON bot_decisions(bot_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_decisions_action "
        "ON bot_decisions(action)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_decisions_created_at "
        "ON bot_decisions(created_at)"
    )

    # Archive tables — same schema as their live counterparts, no FK constraints.
    # Created here so they are always available even before the first cleanup run.
    _create_archive_tables(cursor)

    cursor.execute("SELECT COUNT(*) AS c FROM app_settings")
    if cursor.fetchone()["c"] == 0:
        defaults = [
            ("execution_mode", "testnet"),
            ("global_trading_halted", "false"),
        ]
        cursor.executemany(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            defaults,
        )

    # Per-voter vote log used by MetaMagi to learn dynamic weights.
    # forward_roc_* and realized_pnl are filled later by meta_training_loop.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS voter_feedback (
            feedback_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id             TEXT,
            execution_mode     TEXT,
            timestamp          INTEGER NOT NULL,
            target_asset       TEXT    NOT NULL,
            ensemble_signal    TEXT    NOT NULL,
            voter_name         TEXT    NOT NULL,
            voter_signal       TEXT    NOT NULL,
            confidence         REAL,
            forward_roc_30s    REAL,
            forward_roc_5m     REAL,
            realized_pnl       REAL,
            consensus_score    REAL,
            features_snapshot  TEXT
        )
    """)
    # Migrate existing databases that predate the bot_id / confidence columns.
    for col, col_def in [("bot_id", "TEXT"), ("execution_mode", "TEXT"), ("confidence", "REAL")]:
        try:
            cursor.execute(
                f"ALTER TABLE voter_feedback ADD COLUMN {col} {col_def}"
            )
        except Exception:
            pass  # column already exists — safe to ignore
    try:
        cursor.execute(
            "UPDATE voter_feedback SET execution_mode = 'testnet' WHERE execution_mode IS NULL"
        )
    except Exception:
        pass
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voter_feedback_asset_ts "
        "ON voter_feedback(target_asset, timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voter_feedback_bot_ts "
        "ON voter_feedback(bot_id, timestamp)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voter_feedback_bot_mode_ts "
        "ON voter_feedback(bot_id, execution_mode, timestamp)"
    )
    # Per-voter-name lookups for MetaMagi training and feature extraction.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_voter_feedback_voter_ts "
        "ON voter_feedback(voter_name, timestamp)"
    )

    # Cached OHLCV candles — written by bot_runner on every fetch so the
    # backtesting engine can replay any historical window without hitting
    # the Binance API.  ts_open is the candle open timestamp in ms.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_candles (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol    TEXT    NOT NULL,
            timeframe TEXT    NOT NULL,
            ts_open   INTEGER NOT NULL,
            open      REAL,
            high      REAL,
            low       REAL,
            close     REAL,
            volume    REAL,
            UNIQUE(symbol, timeframe, ts_open)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_ohlcv_sym_tf_ts "
        "ON ohlcv_candles(symbol, timeframe, ts_open)"
    )

    # Per-entry strategy ledger for strategies that pyramid and need to close
    # individual fills independently instead of using aggregate FIFO position.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_open_entries (
            entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            execution_mode TEXT NOT NULL DEFAULT 'testnet',
            symbol TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            exchange_order_id TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (bot_id) REFERENCES bots(bot_id)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_open_entries_bot_symbol "
        "ON strategy_open_entries(bot_id, execution_mode, symbol, created_at)"
    )

    conn.commit()
    conn.close()


def record_bot_order(bot_id: str, execution_mode: str, order: dict[str, Any]) -> None:
    """Store a CCXT order response after the exchange accepts the order."""
    now = int(time.time() * 1000)
    oid = order.get("id")
    raw = json.dumps(order, default=str)
    if len(raw) > 32000:
        raw = raw[:32000] + "…"

    avg = _f(order.get("average"))
    if avg is None:
        avg = _derive_execution_average(order)
    st = order.get("status")
    st_str = str(st).strip() if st is not None else ""

    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO bot_orders (
                bot_id, execution_mode, exchange_order_id, symbol, side, order_type,
                amount, cost, average, filled, status, raw_response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_id,
                execution_mode,
                str(oid) if oid is not None else None,
                str(order.get("symbol") or ""),
                str(order.get("side") or "").lower(),
                str(order.get("type") or ""),
                order.get("amount"),
                order.get("cost"),
                avg,
                order.get("filled"),
                st_str if st_str else None,
                raw,
                now,
            ),
        )
        conn.commit()
    except Exception:
        # Order may already be live on the exchange; do not fail the runner.
        print(
            f"[record_bot_order] FAILED bot_id={bot_id!r}: {traceback.format_exc()}",
            file=sys.stderr,
        )
    finally:
        conn.close()


def upsert_ohlcv_candles(
    symbol: str,
    timeframe: str,
    candles: list[list],
) -> None:
    """
    Persist a batch of CCXT-format OHLCV candles to ``ohlcv_candles``.

    ``candles`` is the list returned by ``exchange.fetch_ohlcv()``:
    ``[[ts_open_ms, open, high, low, close, volume], ...]``

    Uses INSERT OR IGNORE so duplicate candles (same symbol+timeframe+ts_open)
    are silently skipped.  Failures are swallowed — a missing candle is never
    worth interrupting the live trading loop.
    """
    if not candles:
        return
    rows = [
        (symbol, timeframe, int(c[0]), c[1], c[2], c[3], c[4], c[5])
        for c in candles
        if len(c) >= 6
    ]
    if not rows:
        return
    t0 = time.perf_counter()
    conn = get_db_connection()
    try:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ohlcv_candles
              (symbol, timeframe, ts_open, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    except Exception:
        pass  # never interrupt the live trading path
    finally:
        conn.close()
    _report_db_timing(f"upsert_ohlcv_candles({symbol},{timeframe},{len(rows)})", (time.perf_counter() - t0) * 1000)


def sync_bot_orders_from_logs(bot_id: str, symbol: str) -> int:
    """
    Insert bot_orders rows from log lines (same DB) when orders ran before persistence
    existed or insert failed. Idempotent per (bot_id, exchange_order_id).
    """
    conn = get_db_connection()
    inserted = 0
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT created_at, execution_mode, message FROM bot_logs
            WHERE bot_id = ?
              AND (
                message LIKE '%BUY %'
                OR message LIKE '%SELL %'
              )
              AND (
                message LIKE '%market order placed%'
                OR message LIKE '%filled/accepted%'
              )
            ORDER BY created_at ASC
            """,
            (bot_id,),
        )
        for row in cur.fetchall():
            ts = int(row["created_at"])
            ex_mode = str(row["execution_mode"])
            msg = str(row["message"])
            m_buy = _RE_BUY_FROM_LOG.search(msg)
            if m_buy:
                ex_id, q_qty, st = m_buy.group(1), m_buy.group(2), m_buy.group(3)
                cur.execute(
                    """
                    SELECT 1 FROM bot_orders
                    WHERE bot_id = ? AND exchange_order_id = ? AND execution_mode = ?
                    """,
                    (bot_id, ex_id, ex_mode),
                )
                if cur.fetchone():
                    continue
                try:
                    cost_v = float(q_qty)
                except ValueError:
                    cost_v = None
                raw = json.dumps(
                    {"source": "bot_logs_backfill", "message": msg[:2000]},
                    default=str,
                )
                cur.execute(
                    """
                    INSERT INTO bot_orders (
                        bot_id, execution_mode, exchange_order_id, symbol, side, order_type,
                        amount, cost, average, filled, status, raw_response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bot_id,
                        ex_mode,
                        ex_id,
                        symbol,
                        "buy",
                        "market",
                        None,
                        cost_v,
                        None,
                        None,
                        (st or "FILLED"),
                        raw,
                        ts,
                    ),
                )
                inserted += 1
                continue

            m_sell = _RE_SELL_FROM_LOG.search(msg)
            if m_sell:
                ex_id, amt_s, st = m_sell.group(1), m_sell.group(2), m_sell.group(3)
                cur.execute(
                    """
                    SELECT 1 FROM bot_orders
                    WHERE bot_id = ? AND exchange_order_id = ? AND execution_mode = ?
                    """,
                    (bot_id, ex_id, ex_mode),
                )
                if cur.fetchone():
                    continue
                try:
                    amt_v = float(amt_s)
                except ValueError:
                    amt_v = None
                raw = json.dumps(
                    {"source": "bot_logs_backfill", "message": msg[:2000]},
                    default=str,
                )
                cur.execute(
                    """
                    INSERT INTO bot_orders (
                        bot_id, execution_mode, exchange_order_id, symbol, side, order_type,
                        amount, cost, average, filled, status, raw_response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bot_id,
                        ex_mode,
                        ex_id,
                        symbol,
                        "sell",
                        "market",
                        amt_v,
                        None,
                        None,
                        amt_v,
                        (st or "FILLED"),
                        raw,
                        ts,
                    ),
                )
                inserted += 1

        if inserted:
            conn.commit()
        return inserted
    finally:
        conn.close()


def _mode_filter(mode: str | None) -> str | None:
    if mode is None or mode == "both":
        return None
    if mode not in ("testnet", "live"):
        raise ValueError("mode must be 'testnet', 'live', or 'both'")
    return mode


def fetch_bot_orders_panel(
    bot_id: str,
    order_limit: int = 50,
    mode: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aggregate stats and recent rows for the bot detail API."""
    mode_filter = _mode_filter(mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*) AS total_orders,
                COALESCE(SUM(CASE WHEN LOWER(side) = 'buy' THEN 1 ELSE 0 END), 0) AS buy_count,
                COALESCE(SUM(CASE WHEN LOWER(side) = 'sell' THEN 1 ELSE 0 END), 0) AS sell_count,
                MAX(created_at) AS last_order_at_ms
            FROM bot_orders
            WHERE bot_id = ?
              AND (? IS NULL OR execution_mode = ?)
            """,
            (bot_id, mode_filter, mode_filter),
        )
        row = cur.fetchone()
        stats = {
            "total_orders": int(row["total_orders"] or 0),
            "buy_count": int(row["buy_count"] or 0),
            "sell_count": int(row["sell_count"] or 0),
            "last_order_at_ms": row["last_order_at_ms"],
        }
        cur.execute(
            """
            SELECT order_row_id, bot_id, execution_mode, exchange_order_id, symbol, side,
                   order_type, amount, cost, average, filled, status, created_at,
                   raw_response_json
            FROM bot_orders
            WHERE bot_id = ?
              AND (? IS NULL OR execution_mode = ?)
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (bot_id, mode_filter, mode_filter, order_limit),
        )
        orders = []
        for r in cur.fetchall():
            d = _row_to_dict(r)
            raw = d.pop("raw_response_json", None)
            d["display_price"] = _display_price_for_order(d, raw)
            d["display_status"] = _display_status_for_order(d, raw)
            orders.append(d)
        return stats, orders
    finally:
        conn.close()


def fork_bot(
    source_bot_id: str,
    *,
    name: str | None = None,
    strategy_params_json: str | None = None,
) -> dict[str, Any]:
    """
    Insert a new bot row cloned from source (same symbol/strategy by default).
    Does not copy bot_orders, bot_logs, or bot_decisions — the new id starts with empty history.
    Source bot and its data are unchanged.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (source_bot_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"bot not found: {source_bot_id!r}")
        src = _row_to_dict(row)
        new_id = secrets.token_hex(8)
        while True:
            cur.execute("SELECT 1 FROM bots WHERE bot_id = ?", (new_id,))
            if cur.fetchone() is None:
                break
            new_id = secrets.token_hex(8)
        now = int(time.time() * 1000)
        new_name = (name.strip() if isinstance(name, str) and name.strip() else None) or (
            f"{src['name']} (copy)"
        )
        params = (
            strategy_params_json
            if strategy_params_json is not None
            else src.get("strategy_params_json")
        )
        cur.execute(
            """
            INSERT INTO bots (bot_id, name, symbol, strategy, strategy_params_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'stopped', ?)
            """,
            (
                new_id,
                new_name,
                src["symbol"],
                src["strategy"],
                params,
                now,
            ),
        )
        cur.execute("SELECT * FROM bot_risk_settings WHERE bot_id = ?", (source_bot_id,))
        risk_row = cur.fetchone()
        if risk_row:
            risk = _row_to_dict(risk_row)
            cur.execute(
                """
                INSERT INTO bot_risk_settings (
                    bot_id, base_risk_pct, dynamic_tiers_json, daily_loss_limit_pct,
                    max_drawdown_pct, consecutive_loss_limit, enable_daily_loss_limit,
                    enable_drawdown_protection, enable_consecutive_loss,
                    enable_dynamic_sizing, enable_volatility_pause,
                    volatility_threshold, drawdown_action, drawdown_reduce_factor,
                    yolo_mode, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    new_id,
                    risk["base_risk_pct"],
                    risk["dynamic_tiers_json"],
                    risk["daily_loss_limit_pct"],
                    risk["max_drawdown_pct"],
                    risk["consecutive_loss_limit"],
                    risk["enable_daily_loss_limit"],
                    risk["enable_drawdown_protection"],
                    risk["enable_consecutive_loss"],
                    risk["enable_dynamic_sizing"],
                    risk["enable_volatility_pause"],
                    risk["volatility_threshold"],
                    risk["drawdown_action"],
                    risk["drawdown_reduce_factor"],
                    risk.get("yolo_mode", 0),
                    now,
                    now,
                ),
            )
        conn.commit()
        return {
            "new_bot_id": new_id,
            "source_bot_id": source_bot_id,
            "name": new_name,
            "symbol": src["symbol"],
            "strategy": src["strategy"],
        }
    finally:
        conn.close()


def fetch_bot_orders_chronological(
    bot_id: str,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """All orders for FIFO PnL (oldest first)."""
    mode_filter = _mode_filter(mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT side, amount, cost, average, filled, created_at, symbol, execution_mode
            FROM bot_orders
            WHERE bot_id = ?
              AND (? IS NULL OR execution_mode = ?)
            ORDER BY created_at ASC, order_row_id ASC
            """,
            (bot_id, mode_filter, mode_filter),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_latest_tick_id(symbol: str) -> int | None:
    """Return the most recent tick_id for the given CCXT symbol, or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT tick_id FROM market_ticks WHERE target_asset = ? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        )
        row = cur.fetchone()
        return int(row["tick_id"]) if row else None
    finally:
        conn.close()


def record_bot_decision(
    bot_id: str,
    symbol: str,
    mode: str,
    action: str,
    confidence: float | None,
    executed: bool,
) -> None:
    """
    Log a BUY / SELL / HOLD decision to bot_decisions for ML labelling.
    Links to the latest market_tick for `symbol` when available.
    """
    tick_id = get_latest_tick_id(symbol)
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO bot_decisions (bot_id, tick_id, mode, action, confidence, executed)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (bot_id, tick_id, mode, action.upper(), confidence, int(executed)),
        )
        conn.commit()
    finally:
        conn.close()


def insert_voter_feedback(record: dict[str, Any]) -> None:
    """
    Persist one voter's vote for a single ensemble decision.

    Required keys: timestamp, target_asset, ensemble_signal, voter_name, voter_signal
    Optional keys: bot_id, execution_mode, confidence, consensus_score, features_snapshot (JSON string)
    Deferred keys: forward_roc_30s, forward_roc_5m, realized_pnl  (filled by training loop)
    """
    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO voter_feedback (
                bot_id, execution_mode, timestamp, target_asset, ensemble_signal,
                voter_name, voter_signal, confidence,
                forward_roc_30s, forward_roc_5m, realized_pnl,
                consensus_score, features_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.get("bot_id"),
                record.get("execution_mode") or "testnet",
                record["timestamp"],
                record["target_asset"],
                record["ensemble_signal"],
                record["voter_name"],
                record["voter_signal"],
                record.get("confidence"),
                record.get("forward_roc_30s"),
                record.get("forward_roc_5m"),
                record.get("realized_pnl"),
                record.get("consensus_score"),
                record.get("features_snapshot"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def batch_insert_bot_logs(records: list[tuple]) -> None:
    """
    Persist many bot_log rows in a single connection + single commit.

    Each tuple must be: (bot_id, created_at_ms, level, execution_mode, message).
    Called once per cycle by _flush_log_queue() in bot_runner — replaces the
    per-line open/commit/close pattern that caused heavy write contention.
    """
    if not records:
        return
    t0 = time.perf_counter()
    conn = get_db_connection()
    try:
        conn.executemany(
            "INSERT INTO bot_logs"
            "  (bot_id, created_at, level, execution_mode, message)"
            "  VALUES (?, ?, ?, ?, ?)",
            records,
        )
        conn.commit()
    finally:
        conn.close()
    _report_db_timing(f"batch_insert_bot_logs({len(records)})", (time.perf_counter() - t0) * 1000)


def batch_insert_voter_feedback(records: list[dict[str, Any]]) -> None:
    """
    Persist many voter votes in a single connection + single commit.

    Replaces calling insert_voter_feedback() in a tight loop.  With 15 bots and
    8 voters each, this drops ~120 separate transactions per 5-second cycle down
    to one — eliminating the primary SQLite writer-lock contention source.

    Accepts the same dict shape as insert_voter_feedback().
    """
    if not records:
        return
    rows = [
        (
            r.get("bot_id"),
            r.get("execution_mode") or "testnet",
            r["timestamp"],
            r["target_asset"],
            r["ensemble_signal"],
            r["voter_name"],
            r["voter_signal"],
            r.get("confidence"),
            r.get("forward_roc_30s"),
            r.get("forward_roc_5m"),
            r.get("realized_pnl"),
            r.get("consensus_score"),
            r.get("features_snapshot"),
        )
        for r in records
    ]
    t0 = time.perf_counter()
    conn = get_db_connection()
    try:
        conn.executemany(
            """
            INSERT INTO voter_feedback (
                bot_id, execution_mode, timestamp, target_asset, ensemble_signal,
                voter_name, voter_signal, confidence,
                forward_roc_30s, forward_roc_5m, realized_pnl,
                consensus_score, features_snapshot
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    _report_db_timing(f"batch_insert_voter_feedback({len(records)})", (time.perf_counter() - t0) * 1000)


def batch_record_bot_decisions(decisions: list[dict[str, Any]]) -> None:
    """
    Insert many bot_decisions rows in a single connection + single commit.

    Each dict must contain: bot_id, symbol, mode, action, confidence, executed.
    tick_id is resolved for all unique symbols in one query before the bulk insert,
    so the entire batch costs one connection and two queries regardless of length.

    Thread-safe: callers build their own list per bot per cycle; there is no
    shared mutable state between threads.
    """
    if not decisions:
        return

    now_ms = int(time.time() * 1000)
    t0 = time.perf_counter()
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # Resolve the latest tick_id for each unique symbol in a single connection.
        unique_symbols = {d["symbol"] for d in decisions}
        tick_map: dict[str, int | None] = {}
        for sym in unique_symbols:
            cur.execute(
                "SELECT tick_id FROM market_ticks "
                "WHERE target_asset = ? ORDER BY timestamp DESC LIMIT 1",
                (sym,),
            )
            row = cur.fetchone()
            tick_map[sym] = int(row["tick_id"]) if row else None

        rows = [
            (
                d["bot_id"],
                tick_map.get(d["symbol"]),
                d["mode"],
                d["action"].upper(),
                d.get("confidence"),
                int(d["executed"]),
                now_ms,
            )
            for d in decisions
        ]
        cur.executemany(
            """
            INSERT INTO bot_decisions
              (bot_id, tick_id, mode, action, confidence, executed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()
    _report_db_timing(
        f"batch_record_bot_decisions({len(decisions)})",
        (time.perf_counter() - t0) * 1000,
    )


def get_latest_voter_signals(
    bot_id: str,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return the most-recent signal for each voter for a given bot.

    Used by the Bot Detail page to render live voter cards.
    Each row: voter_name, voter_signal, confidence, consensus_score, timestamp.
    """
    mode_filter = _mode_filter(mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT vf.voter_name,
                   vf.voter_signal,
                   vf.confidence,
                   vf.consensus_score,
                   vf.execution_mode,
                   vf.timestamp
            FROM voter_feedback vf
            INNER JOIN (
                SELECT voter_name, MAX(timestamp) AS max_ts
                FROM voter_feedback
                WHERE bot_id = ?
                  AND (? IS NULL OR execution_mode = ?)
                GROUP BY voter_name
            ) latest
              ON vf.voter_name = latest.voter_name
             AND vf.timestamp  = latest.max_ts
             AND vf.bot_id     = ?
             AND (? IS NULL OR vf.execution_mode = ?)
            ORDER BY vf.voter_name ASC
            """,
            (bot_id, mode_filter, mode_filter, bot_id, mode_filter, mode_filter),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_voter_feedback_batch(
    hours: int = 24,
    mode: str | None = None,
) -> list[dict[str, Any]]:
    """Return labeled + unlabeled voter_feedback rows from the last `hours` hours."""
    cutoff_ms = int((time.time() - hours * 3600) * 1000)
    mode_filter = _mode_filter(mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT feedback_id, execution_mode, timestamp, target_asset, ensemble_signal,
                   voter_name, voter_signal,
                   forward_roc_30s, forward_roc_5m, realized_pnl,
                   consensus_score, features_snapshot
            FROM voter_feedback
            WHERE timestamp >= ?
              AND (? IS NULL OR execution_mode = ?)
            ORDER BY timestamp ASC
            """,
            (cutoff_ms, mode_filter, mode_filter),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _roc_for_window(
    cur: sqlite3.Cursor,
    target_asset: str,
    timestamp_ms: int,
    window_ms: int,
) -> float | None:
    """Return forward ROC for one feedback row/window using indexed lookups."""
    cur.execute(
        """
        SELECT target_price FROM market_ticks
        WHERE target_asset = ?
          AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (target_asset, timestamp_ms),
    )
    base = cur.fetchone()
    cur.execute(
        """
        SELECT target_price FROM market_ticks
        WHERE target_asset = ?
          AND timestamp >= ?
        ORDER BY timestamp ASC
        LIMIT 1
        """,
        (target_asset, timestamp_ms + window_ms),
    )
    fwd = cur.fetchone()
    if not base or not fwd:
        return None
    base_price = float(base["target_price"] or 0)
    fwd_price = float(fwd["target_price"] or 0)
    if base_price <= 0:
        return None
    return (fwd_price - base_price) / base_price


def voter_feedback_label_window_bounds(
    lookback_minutes: int | None,
) -> tuple[int, int]:
    """`(cutoff_ms, upper_ms)` aligned with ``label_voter_feedback_forward_roc_batch``."""
    if lookback_minutes is None:
        cutoff_ms = 0
    else:
        cutoff_ms = int((time.time() - lookback_minutes * 60) * 1000)
    trailing_gap_ms = 300_000
    upper_ms = int(time.time() * 1000) - trailing_gap_ms
    return cutoff_ms, upper_ms


def count_voter_feedback_unlabeled(
    *,
    lookback_minutes: int | None,
    busy_timeout_ms: int = 500,
) -> int:
    """Rows in the label window that still need at least one forward ROC field."""
    cutoff_ms, upper_ms = voter_feedback_label_window_bounds(lookback_minutes)
    conn = get_direct_db_connection(timeout_ms=busy_timeout_ms)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM voter_feedback
            WHERE (forward_roc_30s IS NULL OR forward_roc_5m IS NULL)
              AND timestamp >= ?
              AND timestamp <= ?
            """,
            (cutoff_ms, upper_ms),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()


def label_voter_feedback_forward_roc_batch(
    *,
    lookback_minutes: int | None = 180,
    batch_size: int = 50,
    busy_timeout_ms: int = 250,
) -> dict[str, Any]:
    """Label one small voter_feedback batch without monopolizing SQLite.

    The previous implementation ran two broad UPDATE statements over a large
    window and held SQLite's writer lock long enough to freeze live bots. This
    function does indexed reads first, then commits one tiny update batch.

    ``lookback_minutes=None`` scans from the beginning of time (``timestamp >= 0``)
    for catch-up jobs; an integer limits rows to those newer than that window.
    """
    cutoff_ms, upper_ms = voter_feedback_label_window_bounds(lookback_minutes)
    t0 = time.perf_counter()
    result: dict[str, Any] = {
        "selected": 0,
        "updated_30s": 0,
        "updated_5m": 0,
        "db_busy": False,
        "elapsed_ms": 0.0,
    }
    conn = get_direct_db_connection(timeout_ms=busy_timeout_ms)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT feedback_id, timestamp, target_asset,
                   forward_roc_30s, forward_roc_5m
            FROM voter_feedback
            WHERE (forward_roc_30s IS NULL OR forward_roc_5m IS NULL)
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (cutoff_ms, upper_ms, max(1, batch_size)),
        )
        rows = [dict(row) for row in cur.fetchall()]
        result["selected"] = len(rows)
        updates: list[tuple[float | None, float | None, int]] = []
        for row in rows:
            roc_30s = row["forward_roc_30s"]
            roc_5m = row["forward_roc_5m"]
            if roc_30s is None:
                roc_30s = _roc_for_window(
                    cur,
                    row["target_asset"],
                    int(row["timestamp"]),
                    30_000,
                )
            if roc_5m is None:
                roc_5m = _roc_for_window(
                    cur,
                    row["target_asset"],
                    int(row["timestamp"]),
                    300_000,
                )
            if roc_30s is not None or roc_5m is not None:
                updates.append((roc_30s, roc_5m, int(row["feedback_id"])))
                if row["forward_roc_30s"] is None and roc_30s is not None:
                    result["updated_30s"] += 1
                if row["forward_roc_5m"] is None and roc_5m is not None:
                    result["updated_5m"] += 1
        if updates:
            cur.executemany(
                """
                UPDATE voter_feedback
                SET forward_roc_30s = COALESCE(?, forward_roc_30s),
                    forward_roc_5m = COALESCE(?, forward_roc_5m)
                WHERE feedback_id = ?
                """,
                updates,
            )
            conn.commit()
        return result
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            result["db_busy"] = True
            return result
        raise
    finally:
        result["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
        conn.close()


def label_voter_feedback_forward_roc(lookback_minutes: int = 60) -> int:
    """Compatibility wrapper: label bounded batches instead of one huge write."""
    total = 0
    deadline = time.monotonic() + float(
        os.environ.get("METAMAGI_MANUAL_LABEL_MAX_SECONDS", "30")
    )
    while time.monotonic() < deadline:
        batch = label_voter_feedback_forward_roc_batch(
            lookback_minutes=lookback_minutes,
            batch_size=int(os.environ.get("METAMAGI_LABEL_BATCH_SIZE", "100")),
            busy_timeout_ms=int(
                os.environ.get("METAMAGI_DB_BUSY_TIMEOUT_MS", "500")
            ),
        )
        if batch.get("db_busy") or int(batch.get("selected") or 0) == 0:
            break
        updated = max(
            int(batch.get("updated_30s") or 0),
            int(batch.get("updated_5m") or 0),
        )
        if updated == 0:
            break
        total += updated
    return total


def metamagi_label_voter_feedback_catchup(
    *,
    lookback_minutes: int | None = None,
    batch_size: int | None = None,
    busy_timeout_ms: int | None = None,
    max_seconds: float | None = None,
    max_batches: int | None = None,
    batch_sleep_sec: float | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Label `voter_feedback` until nothing eligible remains (within scan window).

    Runs batch after batch until a batch selects **zero** rows (`stopped_reason`
    ``no_rows``), meaning there are no rows left that still need ROC fields inside
    the chosen lookback (or entire table when ``lookback_minutes`` is ``None``).

    Unlike `_meta_training_loop`, uses ``lookback_minutes=None`` by default (via env)
    so the scan covers **all history**, not only the last few hours.

    Optional caps ``max_seconds`` / ``max_batches`` (API or env
    ``METAMAGI_CATCHUP_MAX_SECONDS`` / ``METAMAGI_CATCHUP_MAX_BATCHES``) truncate a
    run for emergencies; leave unset or ``0`` / ``unlimited`` for no cap.

    On SQLite ``db_busy``, sleeps and retries (see ``METAMAGI_CATCHUP_MAX_CONSECUTIVE_DB_BUSY``).

    Emits structured logs on logger ``metamagi_catchup``.

    Optional ``progress_callback`` receives dict events for streaming UIs:
    ``start``, ``progress``, ``db_busy`` (shape documented at emit sites).
    """
    log = _metamagi_catchup_log

    def _emit(event: dict[str, Any]) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(event)
        except Exception:
            log.exception(
                "MetaMagi catch-up progress_callback failed — continuing labeling"
            )

    try:
        # Scan window: explicit arg wins; env empty / all / full / 0 => entire table
        if lookback_minutes is not None:
            lb: int | None = lookback_minutes
        else:
            raw_lb = os.environ.get("METAMAGI_CATCHUP_LOOKBACK_MINUTES", "").strip().lower()
            if raw_lb in ("", "all", "full", "0"):
                lb = None
            else:
                try:
                    lb = int(raw_lb)
                except ValueError:
                    log.warning(
                        "METAMAGI_CATCHUP_LOOKBACK_MINUTES=%r invalid — using full table",
                        raw_lb,
                    )
                    lb = None

        bs = (
            batch_size
            if batch_size is not None
            else int(os.environ.get("METAMAGI_CATCHUP_BATCH_SIZE", "120"))
        )
        bt = (
            busy_timeout_ms
            if busy_timeout_ms is not None
            else int(os.environ.get("METAMAGI_CATCHUP_DB_BUSY_TIMEOUT_MS", "500"))
        )

        if max_seconds is not None:
            cap_sec: float | None = max_seconds
        else:
            raw_sec = os.environ.get("METAMAGI_CATCHUP_MAX_SECONDS", "").strip().lower()
            cap_sec = (
                None
                if raw_sec in ("", "0", "none", "unlimited")
                else float(raw_sec)
            )

        if max_batches is not None:
            cap_batches: int | None = max_batches
        else:
            raw_mb = os.environ.get("METAMAGI_CATCHUP_MAX_BATCHES", "").strip().lower()
            cap_batches = (
                None
                if raw_mb in ("", "0", "none", "unlimited")
                else int(raw_mb)
            )

        sleep_s = (
            batch_sleep_sec
            if batch_sleep_sec is not None
            else float(os.environ.get("METAMAGI_CATCHUP_BATCH_SLEEP_SEC", "0.05"))
        )
        busy_retry_sleep = float(
            os.environ.get("METAMAGI_CATCHUP_DB_BUSY_RETRY_SLEEP_SEC", "0.25")
        )
        max_consecutive_busy = int(
            os.environ.get("METAMAGI_CATCHUP_MAX_CONSECUTIVE_DB_BUSY", "0")
        )

        lb_desc = "full_table" if lb is None else str(lb)
        log.info(
            "MetaMagi catch-up starting lookback_minutes=%s batch_size=%d "
            "busy_timeout_ms=%d max_seconds=%s max_batches=%s batch_sleep_sec=%.3f "
            "db_busy_retry_sleep=%.3f max_consecutive_db_busy=%d",
            lb_desc,
            bs,
            bt,
            cap_sec,
            cap_batches,
            sleep_s,
            busy_retry_sleep,
            max_consecutive_busy,
        )

        initial_remaining = count_voter_feedback_unlabeled(
            lookback_minutes=lb, busy_timeout_ms=bt
        )
        last_remaining = initial_remaining
        _emit(
            {
                "type": "start",
                "unlabeled_remaining": initial_remaining,
                "lookback_scan": "full_table" if lb is None else "window",
                "lookback_minutes": lb,
            }
        )
        log.info(
            "MetaMagi catch-up unlabeled rows in scan window at start: %d",
            initial_remaining,
        )

        deadline = (
            time.monotonic() + float(cap_sec) if cap_sec is not None else None
        )
        t0 = time.perf_counter()
        batches_run = 0
        selected_total = 0
        updated_30s_total = 0
        updated_5m_total = 0
        consecutive_busy = 0
        stop_reason = "no_rows"

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                stop_reason = "max_seconds"
                log.info(
                    "MetaMagi catch-up stopping: max_seconds=%s cap — "
                    "omit METAMAGI_CATCHUP_MAX_SECONDS for a full drain",
                    cap_sec,
                )
                break
            if cap_batches is not None and batches_run >= cap_batches:
                stop_reason = "max_batches"
                log.info(
                    "MetaMagi catch-up stopping: max_batches=%s cap — "
                    "omit METAMAGI_CATCHUP_MAX_BATCHES for a full drain",
                    cap_batches,
                )
                break

            batch = label_voter_feedback_forward_roc_batch(
                lookback_minutes=lb,
                batch_size=bs,
                busy_timeout_ms=bt,
            )

            if batch.get("db_busy"):
                consecutive_busy += 1
                log.warning(
                    "MetaMagi catch-up SQLite busy consecutive=%d sleeping %.3fs",
                    consecutive_busy,
                    busy_retry_sleep,
                )
                if max_consecutive_busy > 0 and consecutive_busy >= max_consecutive_busy:
                    stop_reason = "db_busy"
                    log.error(
                        "MetaMagi catch-up stopping: %d consecutive db_busy "
                        "(set METAMAGI_CATCHUP_MAX_CONSECUTIVE_DB_BUSY=0 to retry "
                        "without limit)",
                        consecutive_busy,
                    )
                    break
                _emit(
                    {
                        "type": "db_busy",
                        "consecutive_busy": consecutive_busy,
                        "unlabeled_remaining": last_remaining,
                    }
                )
                time.sleep(busy_retry_sleep)
                continue

            consecutive_busy = 0
            batches_run += 1
            sel = int(batch.get("selected") or 0)
            u30 = int(batch.get("updated_30s") or 0)
            u5 = int(batch.get("updated_5m") or 0)
            batch_ms = float(batch.get("elapsed_ms") or 0.0)
            selected_total += sel
            updated_30s_total += u30
            updated_5m_total += u5

            remaining = count_voter_feedback_unlabeled(
                lookback_minutes=lb, busy_timeout_ms=bt
            )
            last_remaining = remaining
            _emit(
                {
                    "type": "progress",
                    "batches_run": batches_run,
                    "unlabeled_remaining": remaining,
                    "batch_selected": sel,
                    "updated_forward_roc_30s": u30,
                    "updated_forward_roc_5m": u5,
                    "batch_elapsed_ms": round(batch_ms, 3),
                }
            )

            cap_b_txt = str(cap_batches) if cap_batches is not None else "∞"
            log.info(
                "MetaMagi catch-up batch n=%s/%s selected=%d updated_30s=%d "
                "updated_5m=%d batch_elapsed_ms=%.1f unlabeled_remaining=%d",
                batches_run,
                cap_b_txt,
                sel,
                u30,
                u5,
                batch_ms,
                remaining,
            )

            if sel == 0:
                stop_reason = "no_rows"
                log.info(
                    "MetaMagi catch-up stopping: no unlabeled rows in scan window "
                    "(batch_passes=%d)",
                    batches_run,
                )
                break
            if u30 == 0 and u5 == 0:
                stop_reason = "no_updatable_rows"
                log.warning(
                    "MetaMagi catch-up stopping: selected=%d rows but no ROC "
                    "computed (missing market_ticks for oldest backlog?). batch_pass=%d",
                    sel,
                    batches_run,
                )
                break

            time.sleep(max(0.0, sleep_s))

        updated_cells = updated_30s_total + updated_5m_total
        wall_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        log.info(
            "MetaMagi catch-up finished stopped_reason=%s batches_run=%d "
            "selected_rows_total=%d updated_30s_total=%d updated_5m_total=%d "
            "updated_label_cells=%d wall_ms=%.1f",
            stop_reason,
            batches_run,
            selected_total,
            updated_30s_total,
            updated_5m_total,
            updated_cells,
            wall_ms,
        )
        return {
            "lookback_minutes": lb,
            "lookback_scan": "full_table" if lb is None else "window",
            "batch_size": bs,
            "busy_timeout_ms": bt,
            "max_seconds_cap": cap_sec,
            "max_batches_cap": cap_batches,
            "batches_run": batches_run,
            "selected_rows": selected_total,
            "updated_forward_roc_30s": updated_30s_total,
            "updated_forward_roc_5m": updated_5m_total,
            "updated_label_cells": updated_cells,
            "stopped_reason": stop_reason,
            "elapsed_ms": wall_ms,
            "unlabeled_remaining_at_end": last_remaining,
        }
    except Exception:
        log.exception(
            "MetaMagi catch-up aborted: unexpected error "
            "(lookback_minutes=%r batch_size=%r max_seconds=%r)",
            lookback_minutes,
            batch_size,
            max_seconds,
        )
        raise


def peer_voter_weights_for_new_ensemble_bot(
    strategy: str,
    voters: list[str],
) -> dict[str, float] | None:
    """
    Find an existing bot with the same ``strategy`` and identical voter set (order-independent),
    preferring the one with the most ``voter_feedback`` rows. Returns that bot's ``voter_weights``
    restricted to ``voters``, or ``None`` if no suitable peer exists.

    Used when creating a new ensemble bot so static weights start from data-rich peers instead of
    cold template defaults.
    """
    if not voters or not strategy.strip():
        return None
    target_key = tuple(sorted(str(v) for v in voters))
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT bot_id, strategy_params_json FROM bots WHERE strategy = ?",
            (strategy,),
        )
        peers: dict[str, dict[str, float]] = {}
        for row in cur.fetchall():
            raw = row["strategy_params_json"]
            if raw is None or not str(raw).strip():
                continue
            try:
                pj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(pj, dict):
                continue
            vv = pj.get("voters")
            if not isinstance(vv, list) or not vv:
                continue
            if not all(isinstance(x, str) for x in vv):
                continue
            if tuple(sorted(vv)) != target_key:
                continue
            vw = pj.get("voter_weights")
            if not isinstance(vw, dict) or not vw:
                continue
            cleaned: dict[str, float] = {}
            for k, val in vw.items():
                if not isinstance(k, str):
                    continue
                fv = _f(val)
                if fv is None:
                    continue
                cleaned[k] = fv
            if not cleaned:
                continue
            peers[str(row["bot_id"])] = cleaned
        if not peers:
            return None
        ids = tuple(peers.keys())
        placeholders = ",".join("?" * len(ids))
        cur.execute(
            f"""
            SELECT b.bot_id, COUNT(*) AS fc
            FROM bots b
            LEFT JOIN voter_feedback vf ON vf.bot_id = b.bot_id
            WHERE b.bot_id IN ({placeholders})
            GROUP BY b.bot_id
            ORDER BY fc DESC, b.created_at ASC
            LIMIT 1
            """,
            ids,
        )
        winner = cur.fetchone()
        if winner is None:
            return None
        best_id = str(winner["bot_id"])
        src = peers.get(best_id)
        if not src:
            return None
        out: dict[str, float] = {}
        for v in voters:
            vs = str(v)
            if vs in src:
                out[vs] = round(src[vs], 4)
        return out or None
    finally:
        conn.close()


def create_bot(
    name: str,
    symbol: str,
    strategy: str = "sma_cross",
    strategy_params_json: str | None = None,
    execution_mode: str = "testnet",
    capital_source: str = "budget",
    testnet_initial_capital_quote: float | None = None,
    live_initial_capital_quote: float | None = None,
) -> dict[str, Any]:
    """Create a new bot in testnet mode by default and return its full record."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        new_id = secrets.token_hex(8)
        while True:
            cur.execute("SELECT 1 FROM bots WHERE bot_id = ?", (new_id,))
            if cur.fetchone() is None:
                break
            new_id = secrets.token_hex(8)
        now = int(time.time() * 1000)
        cur.execute(
            """
            INSERT INTO bots
              (bot_id, name, symbol, strategy, strategy_params_json, status,
               execution_mode, capital_source, testnet_initial_capital_quote,
               live_initial_capital_quote, created_at)
            VALUES (?, ?, ?, ?, ?, 'stopped', ?, ?, ?, ?, ?)
            """,
            (new_id, name.strip(), symbol.upper().strip(), strategy, strategy_params_json,
             execution_mode, capital_source, testnet_initial_capital_quote,
             live_initial_capital_quote, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (new_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def get_bot_risk_settings(bot_id: str) -> dict[str, Any] | None:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bot_risk_settings WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_bot_risk_state(bot_id: str) -> dict[str, Any]:
    row = get_bot_risk_settings(bot_id) or {}
    return {
        "consecutive_loss_baseline": int(
            row.get("consecutive_loss_baseline") or 0
        ),
        "daily_loss_baseline_date": row.get("daily_loss_baseline_date"),
        "daily_loss_baseline_pnl": float(
            row.get("daily_loss_baseline_pnl") or 0.0
        ),
        "drawdown_baseline_pct": float(row.get("drawdown_baseline_pct") or 0.0),
        "last_risk_pause_reason": row.get("last_risk_pause_reason"),
        "last_manual_resume_at": row.get("last_manual_resume_at"),
    }


def update_bot_risk_state(bot_id: str, state: dict[str, Any]) -> None:
    allowed = {
        "consecutive_loss_baseline",
        "daily_loss_baseline_date",
        "daily_loss_baseline_pnl",
        "drawdown_baseline_pct",
        "last_risk_pause_reason",
        "last_manual_resume_at",
    }
    updates = [key for key in state if key in allowed]
    if not updates:
        return
    set_clause = ", ".join(f"{key} = ?" for key in updates)
    values = [state[key] for key in updates]
    values.append(int(time.time() * 1000))
    values.append(bot_id)
    conn = get_db_connection()
    try:
        conn.execute(
            f"UPDATE bot_risk_settings SET {set_clause}, updated_at = ? "
            "WHERE bot_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def upsert_bot_risk_settings(bot_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time() * 1000)
    dynamic_tiers = settings.get("dynamic_tiers_json")
    if dynamic_tiers is None:
        dynamic_tiers = json.dumps(settings.get("dynamic_tiers", []), default=str)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM bots WHERE bot_id = ?", (bot_id,))
        if cur.fetchone() is None:
            raise ValueError(f"bot not found: {bot_id!r}")
        cur.execute(
            """
            INSERT INTO bot_risk_settings (
                bot_id, base_risk_pct, dynamic_tiers_json, daily_loss_limit_pct,
                max_drawdown_pct, consecutive_loss_limit, enable_daily_loss_limit,
                enable_drawdown_protection, enable_consecutive_loss,
                enable_dynamic_sizing, enable_volatility_pause,
                volatility_threshold, drawdown_action, drawdown_reduce_factor,
                yolo_mode, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(bot_id) DO UPDATE SET
                base_risk_pct = excluded.base_risk_pct,
                dynamic_tiers_json = excluded.dynamic_tiers_json,
                daily_loss_limit_pct = excluded.daily_loss_limit_pct,
                max_drawdown_pct = excluded.max_drawdown_pct,
                consecutive_loss_limit = excluded.consecutive_loss_limit,
                enable_daily_loss_limit = excluded.enable_daily_loss_limit,
                enable_drawdown_protection = excluded.enable_drawdown_protection,
                enable_consecutive_loss = excluded.enable_consecutive_loss,
                enable_dynamic_sizing = excluded.enable_dynamic_sizing,
                enable_volatility_pause = excluded.enable_volatility_pause,
                volatility_threshold = excluded.volatility_threshold,
                drawdown_action = excluded.drawdown_action,
                drawdown_reduce_factor = excluded.drawdown_reduce_factor,
                yolo_mode = excluded.yolo_mode,
                updated_at = excluded.updated_at
            """,
            (
                bot_id,
                float(settings["base_risk_pct"]),
                str(dynamic_tiers),
                float(settings["daily_loss_limit_pct"]),
                float(settings["max_drawdown_pct"]),
                int(settings["consecutive_loss_limit"]),
                1 if settings.get("enable_daily_loss_limit") else 0,
                1 if settings.get("enable_drawdown_protection") else 0,
                1 if settings.get("enable_consecutive_loss") else 0,
                1 if settings.get("enable_dynamic_sizing") else 0,
                1 if settings.get("enable_volatility_pause") else 0,
                (
                    float(settings["volatility_threshold"])
                    if settings.get("volatility_threshold") is not None
                    else None
                ),
                str(settings.get("drawdown_action") or "reduce"),
                float(settings.get("drawdown_reduce_factor") or 0.5),
                1 if settings.get("yolo_mode") else 0,
                now,
                now,
            ),
        )
        conn.commit()
        cur.execute("SELECT * FROM bot_risk_settings WHERE bot_id = ?", (bot_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def set_bot_execution_mode(bot_id: str, execution_mode: str) -> dict[str, Any]:
    """
    Switch a bot between 'testnet' and 'live'.
    Bot must be stopped — the same strategy code runs on both; only the exchange
    endpoint changes (testnet.binance.vision vs api.binance.com).
    """
    if execution_mode not in ("testnet", "live"):
        raise ValueError("execution_mode must be 'testnet' or 'live'")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"bot not found: {bot_id!r}")
        if row["status"] == "running":
            raise ValueError("stop the bot before changing execution mode")
        cur.execute(
            "UPDATE bots SET execution_mode = ? WHERE bot_id = ?",
            (execution_mode, bot_id),
        )
        conn.commit()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def promote_bot_to_live(
    bot_id: str,
    *,
    initial_capital_quote: float,
) -> dict[str, Any]:
    """Switch a stopped bot to live mode and persist live capital settings."""
    if initial_capital_quote <= 0:
        raise ValueError("initial_capital_quote must be positive")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"bot not found: {bot_id!r}")
        if row["status"] == "running":
            raise ValueError("stop the bot before changing execution mode")
        cur.execute(
            """
            UPDATE bots
            SET execution_mode = 'live',
                capital_source = 'budget',
                live_initial_capital_quote = ?
            WHERE bot_id = ?
            """,
            (initial_capital_quote, bot_id),
        )
        conn.commit()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def record_bot_capital_flow(
    bot_id: str,
    execution_mode: str,
    amount_quote: float,
    flow_type: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Record an external capital flow, e.g. deposit or cash-out withdrawal."""
    if execution_mode not in ("testnet", "live"):
        raise ValueError("execution_mode must be 'testnet' or 'live'")
    if flow_type not in ("deposit", "withdrawal", "adjustment"):
        raise ValueError("flow_type must be deposit, withdrawal, or adjustment")
    if amount_quote <= 0:
        raise ValueError("amount_quote must be positive")
    signed = -amount_quote if flow_type == "withdrawal" else amount_quote
    now = int(time.time() * 1000)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM bots WHERE bot_id = ?", (bot_id,))
        if not cur.fetchone():
            raise ValueError(f"bot not found: {bot_id!r}")
        cur.execute(
            """
            INSERT INTO bot_capital_flows
              (bot_id, execution_mode, amount_quote, flow_type, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (bot_id, execution_mode, signed, flow_type, reason, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM bot_capital_flows WHERE flow_id = ?", (cur.lastrowid,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def get_bot_capital_flows(
    bot_id: str,
    execution_mode: str | None = None,
) -> list[dict[str, Any]]:
    mode_filter = _mode_filter(execution_mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM bot_capital_flows
            WHERE bot_id = ?
              AND (? IS NULL OR execution_mode = ?)
            ORDER BY created_at DESC, flow_id DESC
            """,
            (bot_id, mode_filter, mode_filter),
        )
        return [_row_to_dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_bot_net_capital_flow(bot_id: str, execution_mode: str | None = None) -> float:
    mode_filter = _mode_filter(execution_mode)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COALESCE(SUM(amount_quote), 0) AS net_flow
            FROM bot_capital_flows
            WHERE bot_id = ?
              AND (? IS NULL OR execution_mode = ?)
            """,
            (bot_id, mode_filter, mode_filter),
        )
        row = cur.fetchone()
        return float(row["net_flow"] or 0.0) if row else 0.0
    finally:
        conn.close()


def update_bot(bot_id: str, name: str | None, symbol: str | None) -> dict[str, Any]:
    """Update editable bot fields (name, symbol). Bot must not be running."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"bot not found: {bot_id!r}")
        if row["status"] == "running":
            raise ValueError("stop the bot before editing it")
        updates: list[str] = []
        values: list[Any] = []
        if name is not None and name.strip():
            updates.append("name = ?")
            values.append(name.strip())
        if symbol is not None and symbol.strip():
            updates.append("symbol = ?")
            values.append(symbol.upper().strip())
        if updates:
            values.append(bot_id)
            cur.execute(f"UPDATE bots SET {', '.join(updates)} WHERE bot_id = ?", values)
            conn.commit()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (bot_id,))
        return _row_to_dict(cur.fetchone())
    finally:
        conn.close()


def delete_bot(bot_id: str) -> None:
    """Delete a bot and all its orders/logs. Raises ValueError if not found or running."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status FROM bots WHERE bot_id = ?", (bot_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"bot not found: {bot_id!r}")
        if row["status"] == "running":
            raise ValueError("stop the bot before deleting it")
        cur.execute("DELETE FROM bot_risk_settings WHERE bot_id = ?", (bot_id,))
        cur.execute("DELETE FROM bot_logs WHERE bot_id = ?", (bot_id,))
        cur.execute("DELETE FROM bot_orders WHERE bot_id = ?", (bot_id,))
        cur.execute("DELETE FROM bots WHERE bot_id = ?", (bot_id,))
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
