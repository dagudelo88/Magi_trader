"""
Simple Price Breakout Strategy (#15)

Zero-lag pure momentum: buy on new N-candle closing high; sell on new low.

BUY  — current close is strictly above the highest close of the previous N bars.
SELL — current close is strictly below the lowest close of the previous N bars.
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


def default_params() -> dict[str, Any]:
    return {
        "breakout_period": 20,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 50,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("breakout_period", 20))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    close = closes[-1]
    lookback = closes[-(period + 1):-1]
    high = max(lookback)
    low = min(lookback)

    if close > high:
        signal = "buy"
    elif close < low:
        signal = "sell"
    else:
        signal = "hold"

    position_in_range = (close - low) / (high - low) if high > low else 0.5

    return SignalResult(
        signal,
        {
            "close": round(close, 4),
            "period_high": round(high, 4),
            "period_low": round(low, 4),
            "position_in_range_pct": round(position_in_range * 100, 1),
        },
        n,
        confidence=round(abs(position_in_range - 0.5) * 2, 4),
    )
