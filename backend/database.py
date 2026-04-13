import json
import re
import secrets
import sqlite3
import os
import sys
import time
import traceback
from typing import Any

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

def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
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
            created_at INTEGER NOT NULL
        )
    """)

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

    cursor.execute("SELECT COUNT(*) AS c FROM bots")
    if cursor.fetchone()["c"] == 0:
        now = int(time.time() * 1000)
        seed_bots = [
            (
                "1",
                "Alpha_Trend_v4",
                "BTC/USDT",
                "sma_cross",
                '{"fast_period": 5, "slow_period": 15, "quote_fraction": 0.02, "base_fraction": 0.5, "min_trade_interval_sec": 300}',
                "stopped",
                now,
            ),
            (
                "2",
                "Eth_Momentum_Sim",
                "ETH/USDT",
                "sma_cross",
                '{"fast_period": 5, "slow_period": 15, "quote_fraction": 0.02, "base_fraction": 0.5, "min_trade_interval_sec": 300}',
                "stopped",
                now,
            ),
        ]
        cursor.executemany(
            """INSERT INTO bots (bot_id, name, symbol, strategy, strategy_params_json, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            seed_bots,
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
                    WHERE bot_id = ? AND exchange_order_id = ?
                    """,
                    (bot_id, ex_id),
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
                    WHERE bot_id = ? AND exchange_order_id = ?
                    """,
                    (bot_id, ex_id),
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


def fetch_bot_orders_panel(bot_id: str, order_limit: int = 50) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Aggregate stats and recent rows for the bot detail API."""
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
            """,
            (bot_id,),
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
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (bot_id, order_limit),
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


def fetch_bot_orders_chronological(bot_id: str) -> list[dict[str, Any]]:
    """All orders for FIFO PnL (oldest first)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT side, amount, cost, average, filled, created_at, symbol
            FROM bot_orders
            WHERE bot_id = ?
            ORDER BY created_at ASC, order_row_id ASC
            """,
            (bot_id,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def create_bot(
    name: str,
    symbol: str,
    strategy: str = "sma_cross",
    strategy_params_json: str | None = None,
) -> dict[str, Any]:
    """Create a new bot and return its full record."""
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
            INSERT INTO bots (bot_id, name, symbol, strategy, strategy_params_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'stopped', ?)
            """,
            (new_id, name.strip(), symbol.upper().strip(), strategy, strategy_params_json, now),
        )
        conn.commit()
        cur.execute("SELECT * FROM bots WHERE bot_id = ?", (new_id,))
        return _row_to_dict(cur.fetchone())
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
