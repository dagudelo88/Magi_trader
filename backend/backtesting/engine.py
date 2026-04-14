"""
BacktestEngine — replay historical ohlcv_candles + market_ticks data
through any registered strategy and compute realistic P&L metrics.

Architecture
------------
* Pure replay: no network calls, no DB writes.
* Strategies are called with the same interface as the live bot_runner
  (``evaluate(ohlcv_window, params)``), so every strategy works without
  modification.
* Lag ensembles receive ``params["as_of_ts"]`` which the lag ensemble's
  ``evaluate()`` passes down to ``lag_helpers.get_latest_lag_features()``.
* Position model: simple long-only FIFO.  Each BUY opens a position at the
  candle close; each SELL closes it.  Open positions at end-of-window are
  closed at the final candle close.
* Fees: configurable round-trip percentage (default 0.10 %).

Usage
-----
    from backtesting.engine import BacktestEngine, BacktestConfig

    cfg = BacktestConfig(
        symbol="BTC/USDT",
        strategy_name="magi_ensemble_high",
        timeframe="1m",
        start_ts=1_700_000_000_000,  # ms epoch
        end_ts=1_700_086_400_000,
    )
    result = BacktestEngine().run(cfg)
    print(result.summary())
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field
from typing import Any

# Ensure backend/ is on the path when the engine is invoked from scripts/.
_engine_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_engine_dir)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    """All parameters needed to describe one backtest run."""
    symbol: str
    strategy_name: str
    timeframe: str = "5m"
    ohlcv_limit: int = 200          # lookback window fed to strategy each tick
    lag_lookback_sec: int = 60      # for lag ensemble voters
    start_ts: int = 0               # ms epoch; 0 = earliest available data
    end_ts: int = 0                 # ms epoch; 0 = latest available data
    fee_rt: float = 0.001           # round-trip fee fraction (0.001 = 0.10 %)
    # Any strategy_params overrides (merged on top of default_params).
    strategy_params: dict[str, Any] = field(default_factory=dict)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class Trade:
    entry_ts: int
    entry_price: float
    exit_ts: int = 0
    exit_price: float = 0.0
    pnl_pct: float = 0.0
    open: bool = True

    def close(self, exit_ts: int, exit_price: float, fee_rt: float) -> None:
        self.exit_ts = exit_ts
        self.exit_price = exit_price
        raw = (exit_price - self.entry_price) / self.entry_price
        self.pnl_pct = raw - fee_rt
        self.open = False


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[Trade] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    candles_evaluated: int = 0
    data_gap_warning: str = ""

    # ── Computed metrics ──────────────────────────────────────────────────────

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def total_pnl_pct(self) -> float:
        return sum(t.pnl_pct for t in self.trades) * 100

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl_pct > 0) / len(self.trades)

    @property
    def avg_win_pct(self) -> float:
        wins = [t.pnl_pct * 100 for t in self.trades if t.pnl_pct > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss_pct(self) -> float:
        losses = [t.pnl_pct * 100 for t in self.trades if t.pnl_pct <= 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        equity = 1.0
        peak = 1.0
        max_dd = 0.0
        for t in self.trades:
            equity *= (1 + t.pnl_pct)
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        return max_dd * 100

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe (no risk-free rate)."""
        returns = [t.pnl_pct for t in self.trades]
        if len(returns) < 2:
            return 0.0
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 0 else 1e-9
        # Annualise: assume trades/day ≈ (n_trades / window_days),
        # 252 trading days/year.
        window_days = max(
            1,
            (self.trades[-1].exit_ts - self.trades[0].entry_ts)
            / 86_400_000
            if self.trades else 1,
        )
        trades_per_day = len(self.trades) / window_days
        ann_factor = math.sqrt(trades_per_day * 252)
        return (mean_r / std_r) * ann_factor

    def summary(self) -> str:
        cfg = self.config
        lines = [
            f"Symbol:      {cfg.symbol}",
            f"Strategy:    {cfg.strategy_name}  ({cfg.timeframe})",
            f"Candles:     {self.candles_evaluated}",
            f"Trades:      {self.n_trades}",
            f"Total P&L:   {self.total_pnl_pct:+.3f}%",
            f"Win rate:    {self.win_rate * 100:.1f}%",
            f"Avg win:     {self.avg_win_pct:+.3f}%",
            f"Avg loss:    {self.avg_loss_pct:+.3f}%",
            f"Max DD:      {self.max_drawdown_pct:.2f}%",
            f"Sharpe:      {self.sharpe:.2f}",
        ]
        if self.data_gap_warning:
            lines.append(f"WARNING:     {self.data_gap_warning}")
        return "\n".join(lines)


# ── Data loading ──────────────────────────────────────────────────────────────

def _load_candles(
    symbol: str,
    timeframe: str,
    start_ts: int,
    end_ts: int,
) -> list[list]:
    """
    Load all stored OHLCV candles for ``symbol``/``timeframe`` within the
    requested window from the ``ohlcv_candles`` table.

    Returns CCXT-format list: [[ts_open_ms, open, high, low, close, vol], ...]
    sorted ascending by ts_open.
    """
    from database import get_db_connection  # type: ignore[import]

    conn = get_db_connection()
    try:
        q = "SELECT ts_open, open, high, low, close, volume FROM ohlcv_candles WHERE symbol=? AND timeframe=?"
        params: list[Any] = [symbol, timeframe]
        if start_ts:
            q += " AND ts_open >= ?"
            params.append(start_ts)
        if end_ts:
            q += " AND ts_open <= ?"
            params.append(end_ts)
        q += " ORDER BY ts_open ASC"
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    return [[r[0], r[1], r[2], r[3], r[4], r[5]] for r in rows]


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Replay historical candle data through any registered strategy and
    return a :class:`BacktestResult` with full trade log and metrics.
    """

    def run(self, cfg: BacktestConfig) -> BacktestResult:
        """
        Execute the backtest defined by ``cfg``.

        For each candle in the historical window:
        1.  Build the OHLCV *lookback window* (up to ``ohlcv_limit`` candles
            ending at the current candle).
        2.  If the strategy is a lag ensemble, inject ``as_of_ts`` into params
            so ``lag_helpers`` fetches ticks at the correct historical moment.
        3.  Call ``strategy.evaluate(window, params)`` — identical interface
            to the live bot_runner.
        4.  Simulate BUY/SELL with fees; track equity curve.
        """
        from trading.strategies.registry import (  # type: ignore[import]
            get_strategy,
            default_params_for,
        )

        result = BacktestResult(config=cfg)

        candles = _load_candles(
            cfg.symbol, cfg.timeframe, cfg.start_ts, cfg.end_ts
        )
        if not candles:
            result.data_gap_warning = (
                f"No ohlcv_candles data for {cfg.symbol} {cfg.timeframe}. "
                "Run scripts/seed_ohlcv.py to backfill historical data."
            )
            return result

        strategy = get_strategy(cfg.strategy_name)
        base_params: dict[str, Any] = {
            **default_params_for(cfg.strategy_name),
            **cfg.strategy_params,
            "symbol": cfg.symbol,
        }

        # Detect lag ensembles by checking for the 'target_asset' param key
        # (set in all three lag ensemble default_params).
        is_lag = "target_asset" in default_params_for(cfg.strategy_name)

        open_trade: Trade | None = None

        for idx in range(len(candles)):
            # Build the lookback window ending at (and including) current candle.
            window_start = max(0, idx + 1 - cfg.ohlcv_limit)
            window = candles[window_start: idx + 1]
            if len(window) < 2:
                continue

            current_ts: int = int(candles[idx][0])
            params = dict(base_params)

            if is_lag:
                # Inject historical timestamp so lag_helpers queries ticks
                # that existed at this exact moment during the original run.
                params["as_of_ts"] = current_ts
                params.setdefault("target_asset", cfg.symbol)
                params.setdefault("lag_lookback_sec", cfg.lag_lookback_sec)

            try:
                signal_result = strategy.evaluate(window, params)
            except Exception:
                continue

            close_price = float(candles[idx][4])
            signal = signal_result.signal
            result.candles_evaluated += 1

            result.signals.append({
                "ts": current_ts,
                "signal": signal,
                "close": close_price,
                "consensus_score": (
                    signal_result.meta.get("consensus_score")
                    if isinstance(signal_result.meta, dict) else None
                ),
            })

            if signal == "buy" and open_trade is None:
                open_trade = Trade(
                    entry_ts=current_ts,
                    entry_price=close_price,
                )
            elif signal == "sell" and open_trade is not None:
                open_trade.close(current_ts, close_price, cfg.fee_rt)
                result.trades.append(open_trade)
                open_trade = None

        # Force-close any open position at the last candle.
        if open_trade is not None and candles:
            last_ts = int(candles[-1][0])
            last_price = float(candles[-1][4])
            open_trade.close(last_ts, last_price, cfg.fee_rt)
            result.trades.append(open_trade)

        # Warn if less than 80 % of the requested window has candle data.
        if cfg.start_ts and cfg.end_ts:
            tf_ms = _timeframe_to_ms(cfg.timeframe)
            if tf_ms:
                expected = max(1, (cfg.end_ts - cfg.start_ts) // tf_ms)
                coverage = len(candles) / expected * 100
                if coverage < 80:
                    result.data_gap_warning = (
                        f"Only {coverage:.0f}% candle coverage "
                        f"({len(candles):,}/{expected:,} candles). "
                        "Run scripts/seed_ohlcv.py to fill gaps."
                    )

        return result


# ── Timeframe helper ──────────────────────────────────────────────────────────

_TF_MS: dict[str, int] = {
    "1s": 1_000, "1m": 60_000, "3m": 180_000, "5m": 300_000,
    "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000,
    "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000,
}


def _timeframe_to_ms(tf: str) -> int:
    return _TF_MS.get(tf, 0)
