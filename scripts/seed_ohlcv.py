"""
Seed historical OHLCV candles from Binance into the local ``ohlcv_candles``
table so the BacktestEngine can replay past windows without live API calls.

Uses the public Binance REST API (no API key required).

Usage examples
--------------
# Seed 30 days of 1m candles for BTC/USDT:
    python scripts/seed_ohlcv.py --symbol BTC/USDT --timeframe 1m --days 30

# Seed multiple symbols and timeframes in one pass:
    python scripts/seed_ohlcv.py --symbol BTC/USDT ETH/USDT --timeframe 1m 5m --days 7

# All tracked symbols across common backtest timeframes:
    python scripts/seed_ohlcv.py --all --timeframe 1m 5m 15m 1h --days 14

# Custom date range:
    python scripts/seed_ohlcv.py --symbol BTC/USDT --timeframe 5m \
        --start 2026-03-01 --end 2026-04-01

Notes
-----
- Binance paginates OHLCV at 1,000 candles per request.  The script
  fetches page-by-page with a 0.3 s delay between requests to stay within
  rate limits.
- Progress is printed per page so you can Ctrl-C and resume later;
  already-stored candles are silently skipped (INSERT OR IGNORE).
- 1 month of 1m candles ≈ 43,200 rows ≈ ~45 API requests ≈ ~15 s.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────────────────────
_script_dir = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_script_dir)
_backend_dir = os.path.join(_repo_root, "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
load_dotenv(os.path.join(_repo_root, ".env"))
load_dotenv(os.path.join(_backend_dir, ".env"), override=True)
# ──────────────────────────────────────────────────────────────────────────────

import ccxt
from database import get_db_connection, upsert_ohlcv_candles
from tracked_markets import TRACKED_USDT_STREAM_IDS, stream_id_to_ccxt

PAGE_SIZE = 1_000       # Binance max per request
REQUEST_DELAY = 0.3    # seconds between pages


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> int:
    return int(
        datetime.strptime(s, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


def _days_ago_ms(days: float) -> int:
    return int((time.time() - days * 86_400) * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _all_ccxt_symbols() -> list[str]:
    return [stream_id_to_ccxt(s) for s in TRACKED_USDT_STREAM_IDS]


def _candle_count(symbol: str, timeframe: str) -> int:
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM ohlcv_candles WHERE symbol=? AND timeframe=?",
            (symbol, timeframe),
        ).fetchone()[0]
    finally:
        conn.close()


# ── Fetcher ───────────────────────────────────────────────────────────────────

def seed_symbol(
    ex: ccxt.Exchange,
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
) -> int:
    """
    Paginate through Binance OHLCV history and persist to ``ohlcv_candles``.
    Returns the total number of candles inserted (duplicates excluded by DB).
    """
    tf_ms: dict[str, int] = {
        "1s": 1_000, "1m": 60_000, "3m": 180_000, "5m": 300_000,
        "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
        "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
    }
    candle_ms = tf_ms.get(timeframe)
    if candle_ms is None:
        print(f"  [skip] Unknown timeframe {timeframe!r}")
        return 0

    total_expected = max(1, (end_ts - start_ts) // candle_ms)
    total_inserted = 0
    cursor = start_ts

    while cursor < end_ts:
        try:
            candles = ex.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                since=cursor,
                limit=PAGE_SIZE,
            )
        except Exception as e:
            print(f"  [error] {symbol} {timeframe} @ {cursor}: {e}")
            break

        if not candles:
            break

        upsert_ohlcv_candles(symbol, timeframe, candles)
        batch = len(candles)
        total_inserted += batch

        last_ts = candles[-1][0]
        pct = min(100.0, (last_ts - start_ts) / max(1, end_ts - start_ts) * 100)
        last_dt = datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        print(
            f"  {symbol:<12} {timeframe:<4}  fetched {batch} candles "
            f"up to {last_dt}  ({pct:.0f}% of window, "
            f"~{total_inserted:,}/{total_expected:,} total)",
            end="\r",
            flush=True,
        )

        # Advance cursor past the last returned candle.
        cursor = last_ts + candle_ms

        # Stop if we've passed the end or Binance returned a partial page.
        if last_ts >= end_ts or batch < PAGE_SIZE:
            break

        time.sleep(REQUEST_DELAY)

    print()  # newline after \r progress
    return total_inserted


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed historical OHLCV candles into ohlcv_candles table.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sym_grp = parser.add_mutually_exclusive_group(required=True)
    sym_grp.add_argument("--symbol", nargs="+", metavar="SYM",
                         help="One or more CCXT symbols, e.g. BTC/USDT ETH/USDT.")
    sym_grp.add_argument("--all", action="store_true",
                         help="Seed all symbols tracked by data_collector.")

    parser.add_argument("--timeframe", nargs="+", default=["1m", "5m"],
                        metavar="TF", help="One or more timeframes (default: 1m 5m).")
    parser.add_argument("--days", type=float, default=7,
                        help="Days of history to fetch (default: 7).")
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date (UTC).  Overrides --days.")
    parser.add_argument("--end", metavar="YYYY-MM-DD",
                        help="End date (UTC).  Defaults to now.")

    args = parser.parse_args()

    end_ts = _parse_date(args.end) if args.end else _now_ms()
    start_ts = _parse_date(args.start) if args.start else _days_ago_ms(args.days)

    symbols = _all_ccxt_symbols() if args.all else args.symbol
    timeframes: list[str] = args.timeframe

    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "spot"}})
    ex.load_markets()

    start_dt = datetime.fromtimestamp(start_ts / 1000, tz=timezone.utc).date()
    end_dt = datetime.fromtimestamp(end_ts / 1000, tz=timezone.utc).date()
    print(f"\nSeeding OHLCV: {start_dt} → {end_dt}")
    print(f"Symbols:    {', '.join(symbols)}")
    print(f"Timeframes: {', '.join(timeframes)}\n")

    grand_total = 0
    for sym in symbols:
        for tf in timeframes:
            before = _candle_count(sym, tf)
            n = seed_symbol(ex, sym, tf, start_ts, end_ts)
            after = _candle_count(sym, tf)
            new = after - before
            grand_total += new
            print(
                f"  {sym:<12} {tf:<5}  "
                f"fetched {n:,} pages-total  "
                f"new rows stored: {new:,}  "
                f"(total in DB: {after:,})"
            )

    print(f"\nDone. {grand_total:,} new candles written to ohlcv_candles.\n")


if __name__ == "__main__":
    main()
