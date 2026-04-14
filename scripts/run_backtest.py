"""
Backtest CLI — replay any bot configuration against stored historical data.

Usage examples
--------------
# Backtest a running bot by its bot_id (reads strategy+params from DB):
    python scripts/run_backtest.py --bot-id 8b9284cb0e586f92 --days 7

# Backtest a strategy by name against any symbol:
    python scripts/run_backtest.py --strategy magi_ensemble_high --symbol BTC/USDT --days 14

# Compare all registered bots over the last 3 days:
    python scripts/run_backtest.py --all --days 3

# Custom date range:
    python scripts/run_backtest.py --strategy magi_ensemble_mid --symbol BTC/USDT \
        --start 2026-04-01 --end 2026-04-14

# Adjust fee and consensus threshold:
    python scripts/run_backtest.py --strategy magi_ensemble_high --symbol BTC/USDT \
        --days 7 --fee 0.075 --params '{"consensus_threshold":0.20}'
"""
from __future__ import annotations

import argparse
import json
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
# ──────────────────────────────────────────────────────────────────────────────

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from database import get_db_connection


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> int:
    """Parse YYYY-MM-DD (UTC) → ms epoch."""
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _days_ago_ms(days: int) -> int:
    return int((time.time() - days * 86_400) * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _load_bots_from_db() -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM bots ORDER BY bot_id").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_bot_by_id(bot_id: str) -> dict | None:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bots WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _candle_count(symbol: str, timeframe: str, start_ts: int, end_ts: int) -> int:
    conn = get_db_connection()
    try:
        q = "SELECT COUNT(*) FROM ohlcv_candles WHERE symbol=? AND timeframe=?"
        p: list = [symbol, timeframe]
        if start_ts:
            q += " AND ts_open >= ?"
            p.append(start_ts)
        if end_ts:
            q += " AND ts_open <= ?"
            p.append(end_ts)
        return conn.execute(q, p).fetchone()[0]
    finally:
        conn.close()


# ── Table printer ─────────────────────────────────────────────────────────────

def _print_results(results: list[tuple[str, BacktestResult]]) -> None:
    hdr = (
        f"\n  {'Bot/Strategy':<28} {'Symbol':<12} {'TF':<4} "
        f"{'Candles':>8}  {'Trades':>7}  {'TotalP&L':>9}  "
        f"{'Win%':>6}  {'AvgWin':>8}  {'AvgLoss':>8}  "
        f"{'MaxDD':>7}  {'Sharpe':>7}"
    )
    sep = "  " + "-" * (len(hdr) - 2)
    print(hdr)
    print(sep)
    for label, r in results:
        cfg = r.config
        n = r.n_trades
        warn = " !" if r.data_gap_warning else "  "
        print(
            f"  {label:<28} {cfg.symbol:<12} {cfg.timeframe:<4} "
            f"{r.candles_evaluated:>8,}  {n:>7,}  "
            f"{r.total_pnl_pct:>+8.3f}%  "
            f"{r.win_rate*100:>5.1f}%  "
            f"{r.avg_win_pct:>+7.3f}%  "
            f"{r.avg_loss_pct:>+7.3f}%  "
            f"{r.max_drawdown_pct:>6.2f}%  "
            f"{r.sharpe:>7.2f}{warn}"
        )
    if any(r.data_gap_warning for _, r in results):
        print("\n  ! = data coverage warning — run scripts/seed_ohlcv.py")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay stored OHLCV data through any MagiTrader strategy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "--bot-id", metavar="ID",
        help="Use a specific bot's strategy+params from the DB.",
    )
    src.add_argument(
        "--strategy", metavar="NAME",
        help="Strategy name (e.g. magi_ensemble_high).",
    )
    src.add_argument(
        "--all", action="store_true",
        help="Backtest all bots currently in the DB.",
    )

    parser.add_argument("--symbol", metavar="SYM", help="Trading symbol, e.g. BTC/USDT.")
    parser.add_argument("--timeframe", metavar="TF", default=None,
                        help="OHLCV timeframe override (default: from strategy params).")
    parser.add_argument("--days", type=float, default=7,
                        help="How many days back to test (default: 7).")
    parser.add_argument("--start", metavar="YYYY-MM-DD",
                        help="Start date (UTC).  Overrides --days.")
    parser.add_argument("--end", metavar="YYYY-MM-DD",
                        help="End date (UTC).  Defaults to now.")
    parser.add_argument("--fee", type=float, default=0.10,
                        help="Round-trip fee in %% (default: 0.10).")
    parser.add_argument("--params", metavar="JSON",
                        help="Extra strategy_params as JSON, e.g. '{\"consensus_threshold\":0.2}'.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print each trade after the summary table.")

    args = parser.parse_args()

    # ── Resolve time window ───────────────────────────────────────────────────
    end_ts = _parse_date(args.end) if args.end else _now_ms()
    start_ts = _parse_date(args.start) if args.start else _days_ago_ms(int(args.days))

    fee_rt = args.fee / 100.0
    extra_params: dict = json.loads(args.params) if args.params else {}

    # ── Build configs ─────────────────────────────────────────────────────────
    configs: list[tuple[str, BacktestConfig]] = []

    if args.all:
        bots = _load_bots_from_db()
        if not bots:
            print("No bots found in DB.")
            sys.exit(0)
        for bot in bots:
            raw = bot.get("strategy_params_json") or "{}"
            try:
                sp = json.loads(raw)
            except json.JSONDecodeError:
                sp = {}
            sp.update(extra_params)
            tf = args.timeframe or sp.get("ohlcv_timeframe", "5m")
            cfg = BacktestConfig(
                symbol=bot["symbol"],
                strategy_name=bot["strategy"],
                timeframe=tf,
                ohlcv_limit=int(sp.get("ohlcv_limit", 200)),
                lag_lookback_sec=int(sp.get("lag_lookback_sec", 60)),
                start_ts=start_ts,
                end_ts=end_ts,
                fee_rt=fee_rt,
                strategy_params=sp,
            )
            configs.append((bot["strategy"], cfg))

    elif args.bot_id:
        bot = _load_bot_by_id(args.bot_id)
        if bot is None:
            print(f"Bot {args.bot_id!r} not found in DB.")
            sys.exit(1)
        raw = bot.get("strategy_params_json") or "{}"
        try:
            sp = json.loads(raw)
        except json.JSONDecodeError:
            sp = {}
        sp.update(extra_params)
        tf = args.timeframe or sp.get("ohlcv_timeframe", "5m")
        cfg = BacktestConfig(
            symbol=bot["symbol"],
            strategy_name=bot["strategy"],
            timeframe=tf,
            ohlcv_limit=int(sp.get("ohlcv_limit", 200)),
            lag_lookback_sec=int(sp.get("lag_lookback_sec", 60)),
            start_ts=start_ts,
            end_ts=end_ts,
            fee_rt=fee_rt,
            strategy_params=sp,
        )
        configs.append((bot["strategy"], cfg))

    elif args.strategy:
        if not args.symbol:
            parser.error("--symbol is required when using --strategy.")
        sp = dict(extra_params)
        tf = args.timeframe or sp.get("ohlcv_timeframe", "5m")
        cfg = BacktestConfig(
            symbol=args.symbol,
            strategy_name=args.strategy,
            timeframe=tf,
            ohlcv_limit=int(sp.get("ohlcv_limit", 200)),
            lag_lookback_sec=int(sp.get("lag_lookback_sec", 60)),
            start_ts=start_ts,
            end_ts=end_ts,
            fee_rt=fee_rt,
            strategy_params=sp,
        )
        configs.append((args.strategy, cfg))

    else:
        parser.print_help()
        sys.exit(0)

    # ── Print data availability check ─────────────────────────────────────────
    print(f"\nBacktest window: {datetime.fromtimestamp(start_ts/1000, tz=timezone.utc).date()} → "
          f"{datetime.fromtimestamp(end_ts/1000, tz=timezone.utc).date()}")
    print(f"Fee (RT):        {fee_rt*100:.3f}%\n")

    for label, cfg in configs:
        n_candles = _candle_count(cfg.symbol, cfg.timeframe, start_ts, end_ts)
        print(f"  {cfg.symbol} {cfg.timeframe} ({label}): "
              f"{n_candles:,} candles available")

    if all(_candle_count(c.symbol, c.timeframe, start_ts, end_ts) == 0
           for _, c in configs):
        print(
            "\n  No stored candles found for this window.\n"
            "  Seed historical data first:\n"
            "    python scripts/seed_ohlcv.py --symbol BTC/USDT "
            f"--timeframe 1m --days {int(args.days)}"
        )
        sys.exit(0)

    # ── Run backtests ─────────────────────────────────────────────────────────
    engine = BacktestEngine()
    results: list[tuple[str, BacktestResult]] = []

    for label, cfg in configs:
        print(f"\nRunning: {label} / {cfg.symbol} {cfg.timeframe} …")
        r = engine.run(cfg)
        results.append((label, r))

    # ── Print summary table ───────────────────────────────────────────────────
    print("\n" + "=" * 100)
    print("  BACKTEST RESULTS")
    print("=" * 100)
    _print_results(results)

    # ── Verbose trade list ────────────────────────────────────────────────────
    if args.verbose:
        for label, r in results:
            if not r.trades:
                continue
            print(f"\n  Trades — {label} / {r.config.symbol}:")
            print(f"  {'Entry':>22}  {'Exit':>22}  {'EntryPx':>10}  {'ExitPx':>10}  {'P&L':>8}")
            print("  " + "-" * 78)
            for t in r.trades:
                entry_dt = datetime.fromtimestamp(t.entry_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                exit_dt  = datetime.fromtimestamp(t.exit_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                print(
                    f"  {entry_dt:>22}  {exit_dt:>22}  "
                    f"{t.entry_price:>10.4f}  {t.exit_price:>10.4f}  "
                    f"{t.pnl_pct*100:>+7.3f}%"
                )

    print()


if __name__ == "__main__":
    main()
