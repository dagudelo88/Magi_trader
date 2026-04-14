"""
Reset MagiTrader DB to a clean state.

Default — clears collected data, keeps bot definitions and app settings:
  CLEAR: market_ticks, ohlcv_candles, voter_feedback, bot_decisions,
         bot_logs, market_depth, bot_orders
  KEEP:  bots, app_settings

--full  — also deletes all bots (full factory reset, only app_settings kept)

Run with --confirm to actually execute. Without it, shows a dry-run preview.
"""
import os, sys, sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from database import DB_PATH

DATA_TABLES = [
    "market_ticks",
    "ohlcv_candles",
    "voter_feedback",
    "bot_decisions",
    "bot_logs",
    "market_depth",
    "bot_orders",
]

BOT_TABLES = ["bots"]
KEEP_TABLES = ["app_settings"]


def row_counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in tables
    }


def main() -> None:
    confirm = "--confirm" in sys.argv
    full = "--full" in sys.argv

    clear_tables = DATA_TABLES + (BOT_TABLES if full else [])
    keep_tables = KEEP_TABLES + ([] if full else BOT_TABLES)
    all_tables = clear_tables + keep_tables

    conn = sqlite3.connect(DB_PATH)
    before = row_counts(conn, all_tables)

    mode_label = "FULL RESET (all bots deleted)" if full else "DATA RESET (bots kept)"
    print(f"\n  Mode: {mode_label}")
    print("\n  Table                  Before     Action")
    print("  " + "-" * 52)
    for t in clear_tables:
        print(f"  {t:<23} {before[t]:>8,}  --> CLEAR")
    for t in keep_tables:
        print(f"  {t:<23} {before[t]:>8,}  --> KEEP")

    if not confirm:
        print("\n  DRY RUN — nothing changed.")
        print("  Re-run with --confirm to apply.")
        print("  Add --full to also delete all bots.\n")
        conn.close()
        return

    print("\n  Clearing tables …")
    for t in clear_tables:
        conn.execute(f"DELETE FROM {t}")  # noqa: S608
    conn.commit()
    conn.execute("VACUUM")

    after = row_counts(conn, all_tables)
    print("\n  Table                  Before   After")
    print("  " + "-" * 46)
    for t in clear_tables:
        print(f"  {t:<23} {before[t]:>6,} → {after[t]:>5,}")
    for t in keep_tables:
        print(f"  {t:<23} {before[t]:>6,}   (kept)")

    conn.close()
    print("\n  Done. DB is clean and ready.\n")


if __name__ == "__main__":
    main()
