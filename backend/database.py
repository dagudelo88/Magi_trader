import json
import re
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
                order.get("average"),
                order.get("filled"),
                str(order.get("status") or "") if order.get("status") is not None else None,
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
                        (st or "unknown"),
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
                        (st or "unknown"),
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
                   order_type, amount, cost, average, filled, status, created_at
            FROM bot_orders
            WHERE bot_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (bot_id, order_limit),
        )
        orders = [_row_to_dict(r) for r in cur.fetchall()]
        return stats, orders
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
