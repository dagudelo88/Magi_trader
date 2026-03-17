import sqlite3
import os

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

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
