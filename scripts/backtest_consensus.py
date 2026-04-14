"""
Backtest: replay voter_feedback with new directional_net consensus rules
against actual market_ticks prices.

For each bot/symbol:
  1. Reconstruct what the ensemble WOULD have signalled under directional_net.
  2. Simulate a simple long-only strategy: enter on BUY, exit on SELL or next BUY.
  3. Report per-symbol and aggregate P&L, win rate, Sharpe, max drawdown.

Assumptions:
  - Entry/exit at the close price of the tick closest to the decision timestamp.
  - 0.10% round-trip fee (Binance taker: 0.075% each side, conservative).
  - Position sizing: fixed 1 unit per trade for clean comparison.
  - No leverage.
"""
import sqlite3
import os
import math
from collections import defaultdict
from datetime import datetime, timezone

DB = os.path.join(os.path.dirname(__file__), "..", "data", "magitrader.db")

# ── helpers ───────────────────────────────────────────────────────────────────

def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def fmt_pct(v: float) -> str:
    return f"{v:+.3f}%"


def fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ── consensus simulation ──────────────────────────────────────────────────────

THRESHOLD_BY_BOT: dict[str, float] = {
    # classic ensembles
    "magi_ensemble_high": 0.15,
    "magi_ensemble_mid":  0.20,
    "magi_ensemble_low":  0.25,
    # lag ensembles
    "magi_lag_ensemble_high": 0.20,
    "magi_lag_ensemble_mid":  0.25,
    "magi_lag_ensemble_low":  0.35,
}
FEE_RT = 0.001   # 0.10% round-trip (0.05% in + 0.05% out)


def directional_net(nb: int, ns: int, n: int, threshold: float) -> str:
    if n == 0:
        return "hold"
    net = (nb - ns) / n
    if net > threshold:
        return "buy"
    if net < -threshold:
        return "sell"
    return "hold"


# ── load data ─────────────────────────────────────────────────────────────────

def load_voting_cycles(conn: sqlite3.Connection) -> dict:
    """
    Returns {(bot_id, symbol, strategy): [(timestamp_ms, nb, ns, n), ...]}
    sorted by timestamp ascending.
    """
    rows = conn.execute("""
        SELECT
            vf.bot_id,
            b.symbol,
            b.strategy,
            vf.timestamp,
            SUM(CASE WHEN vf.voter_signal='buy'  THEN 1 ELSE 0 END) AS nb,
            SUM(CASE WHEN vf.voter_signal='sell' THEN 1 ELSE 0 END) AS ns,
            COUNT(*) AS n
        FROM voter_feedback vf
        JOIN bots b ON b.bot_id = vf.bot_id
        GROUP BY vf.bot_id, vf.timestamp
        ORDER BY vf.timestamp ASC
    """).fetchall()

    cycles: dict = defaultdict(list)
    for r in rows:
        key = (r["bot_id"], r["symbol"], r["strategy"])
        cycles[key].append(
            (r["timestamp"], r["nb"], r["ns"], r["n"])
        )
    return dict(cycles)


def load_prices(
    conn: sqlite3.Connection, symbol: str
) -> list[tuple[int, float]]:
    """
    Returns list of (timestamp_ms, price) from market_ticks, sorted ascending.
    Falls back to bot_decisions close_price if market_ticks empty.
    """
    rows = conn.execute("""
        SELECT timestamp, target_price
        FROM market_ticks
        WHERE target_asset = ? AND target_price IS NOT NULL
        ORDER BY timestamp ASC
    """, (symbol,)).fetchall()

    if rows:
        return [(r["timestamp"], r["target_price"]) for r in rows]

    # Fallback: bot_decisions close_price
    rows = conn.execute("""
        SELECT timestamp, close_price
        FROM bot_decisions
        WHERE symbol = ? AND close_price IS NOT NULL
        ORDER BY timestamp ASC
    """, (symbol,)).fetchall()
    return [(r["timestamp"], r["close_price"]) for r in rows]


def get_price_at(
    price_series: list[tuple[int, float]], ts_ms: int
) -> float | None:
    """Binary search: return price at or immediately after ts_ms."""
    lo, hi = 0, len(price_series) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if price_series[mid][0] < ts_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    if lo < len(price_series):
        return price_series[lo][1]
    return None


# ── per-symbol backtest ───────────────────────────────────────────────────────

class Trade:
    __slots__ = ("entry_ts", "entry_price", "exit_ts", "exit_price", "pnl_pct")

    def __init__(self, entry_ts: int, entry_price: float):
        self.entry_ts = entry_ts
        self.entry_price = entry_price
        self.exit_ts: int = 0
        self.exit_price: float = 0.0
        self.pnl_pct: float = 0.0

    def close(self, exit_ts: int, exit_price: float) -> None:
        self.exit_ts = exit_ts
        self.exit_price = exit_price
        raw = (exit_price - self.entry_price) / self.entry_price
        self.pnl_pct = raw - FEE_RT


def backtest_series(
    cycles: list[tuple],
    prices: list[tuple[int, float]],
    threshold: float,
) -> list[Trade]:
    trades: list[Trade] = []
    open_trade: Trade | None = None

    for ts_ms, nb, ns, n in cycles:
        signal = directional_net(nb, ns, n, threshold)
        price = get_price_at(prices, ts_ms)
        if price is None or price <= 0:
            continue

        if signal == "buy" and open_trade is None:
            open_trade = Trade(ts_ms, price)

        elif signal == "sell" and open_trade is not None:
            open_trade.close(ts_ms, price)
            trades.append(open_trade)
            open_trade = None

    # Close any open trade at last available price
    if open_trade is not None and prices:
        last_ts, last_price = prices[-1]
        open_trade.close(last_ts, last_price)
        trades.append(open_trade)

    return trades


def summarise(trades: list[Trade], symbol: str, strategy: str) -> dict:
    if not trades:
        return {
            "symbol": symbol, "strategy": strategy, "n_trades": 0,
            "total_pnl_pct": 0.0, "win_rate": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0,
            "max_dd_pct": 0.0, "sharpe": 0.0,
        }

    total_pnl = sum(t.pnl_pct for t in trades) * 100
    wins = [t.pnl_pct for t in trades if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in trades if t.pnl_pct <= 0]

    win_rate = len(wins) / len(trades) * 100
    avg_win = (sum(wins) / len(wins) * 100) if wins else 0.0
    avg_loss = (sum(losses) / len(losses) * 100) if losses else 0.0

    # equity curve for drawdown
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    equity_curve = []
    for t in trades:
        equity *= (1 + t.pnl_pct)
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak
        if dd > max_dd:
            max_dd = dd

    # Annualised Sharpe (rough — no risk-free rate, trade returns as samples)
    if len(trades) > 1:
        returns = [t.pnl_pct for t in trades]
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 1e-9
        # Annualise assuming ~6 trades/day (rough), 252 trading days
        ann_factor = math.sqrt(252 * 6)
        sharpe = (mean_r / std_r) * ann_factor
    else:
        sharpe = 0.0

    return {
        "symbol": symbol,
        "strategy": strategy,
        "n_trades": len(trades),
        "total_pnl_pct": total_pnl,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "max_dd_pct": max_dd * 100,
        "sharpe": sharpe,
        "equity_final": equity_curve[-1] if equity_curve else 1.0,
    }


# ── main ──────────────────────────────────────────────────────────────────────

def hdr(t: str) -> None:
    print(f"\n{'='*70}\n  {t}\n{'='*70}")


def main() -> None:
    conn = open_db()

    hdr("DATABASE OVERVIEW")
    schema = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    print("  Tables:", [r["name"] for r in schema])

    vf_count = conn.execute("SELECT COUNT(*) FROM voter_feedback").fetchone()[0]
    tick_count = conn.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0]
    print(f"  voter_feedback rows: {vf_count:,}")
    print(f"  market_ticks rows:   {tick_count:,}")

    cycles_by_bot = load_voting_cycles(conn)
    print(f"  Unique (bot, symbol) pairs: {len(cycles_by_bot)}")

    all_results = []

    hdr("PER-BOT BACKTEST RESULTS")
    print(
        f"\n  {'Symbol':<14} {'Strategy':<26} {'Trades':>7}"
        f"  {'Total%':>8}  {'Win%':>6}  {'AvgWin':>8}  "
        f"{'AvgLoss':>8}  {'MaxDD':>7}  {'Sharpe':>7}"
    )
    print("  " + "-" * 98)

    price_cache: dict[str, list] = {}

    for (bot_id, symbol, strategy), cycles in sorted(
        cycles_by_bot.items(), key=lambda x: x[0][2]
    ):
        if symbol not in price_cache:
            price_cache[symbol] = load_prices(conn, symbol)

        prices = price_cache[symbol]
        threshold = THRESHOLD_BY_BOT.get(strategy, 0.20)

        trades = backtest_series(cycles, prices, threshold)
        result = summarise(trades, symbol, strategy)
        all_results.append(result)

        n = result["n_trades"]
        tp = result["total_pnl_pct"]
        wr = result["win_rate"]
        aw = result["avg_win"]
        al = result["avg_loss"]
        md = result["max_dd_pct"]
        sh = result["sharpe"]
        print(
            f"  {symbol:<14} {strategy:<26} {n:>7,}"
            f"  {tp:>+8.3f}%  {wr:>5.1f}%  {aw:>+7.3f}%  "
            f"{al:>+7.3f}%  {md:>6.2f}%  {sh:>7.2f}"
        )

    # Aggregate
    hdr("AGGREGATE SUMMARY")
    total_trades = sum(r["n_trades"] for r in all_results)
    if total_trades == 0:
        print("  No trades generated — check price data availability.")
        conn.close()
        return

    weighted_pnl = sum(r["total_pnl_pct"] for r in all_results)
    all_wins = [r["win_rate"] for r in all_results if r["n_trades"] > 0]
    avg_win_rate = sum(all_wins) / len(all_wins) if all_wins else 0

    print(f"  Total trades simulated:   {total_trades:,}")
    print(f"  Aggregate P&L (sum):      {weighted_pnl:+.3f}%")
    print(f"  Avg win rate (per bot):   {avg_win_rate:.1f}%")

    best = max(all_results, key=lambda r: r["total_pnl_pct"])
    worst = min(all_results, key=lambda r: r["total_pnl_pct"])
    print(f"  Best bot:   {best['symbol']} / {best['strategy']}  "
          f"({best['total_pnl_pct']:+.3f}%)")
    print(f"  Worst bot:  {worst['symbol']} / {worst['strategy']}  "
          f"({worst['total_pnl_pct']:+.3f}%)")

    # Data quality warnings
    hdr("DATA QUALITY NOTES")
    for (bot_id, symbol, strategy), cycles in cycles_by_bot.items():
        prices = price_cache.get(symbol, [])
        if not prices:
            print(f"  WARNING: No price data for {symbol}")
        else:
            ts_start = cycles[0][0]
            ts_end = cycles[-1][0]
            p_start = prices[0][0]
            p_end = prices[-1][0]
            coverage = (
                min(ts_end, p_end) - max(ts_start, p_start)
            ) / max(ts_end - ts_start, 1) * 100
            if coverage < 80:
                print(
                    f"  WARNING: {symbol} price coverage only "
                    f"{coverage:.0f}% of voting window"
                )

    conn.close()
    print("\nDone.\n")


if __name__ == "__main__":
    main()
