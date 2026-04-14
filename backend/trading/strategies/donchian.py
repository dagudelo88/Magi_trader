"""
Donchian Channel Breakout Strategy (#11)

Pure price-action breakout: new N-period high → BUY; new N-period low → SELL.

BUY  — current close is a new `channel_period`-bar high.
SELL — current close is a new `channel_period`-bar low.
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


def default_params() -> dict[str, Any]:
    return {
        "channel_period": 20,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 50,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("channel_period", 20))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    close = closes[-1]
    # Use previous `period` bars (excluding current) to define the channel
    lookback = closes[-(period + 1):-1]
    channel_high = max(lookback)
    channel_low = min(lookback)

    if close > channel_high:
        signal = "buy"
    elif close < channel_low:
        signal = "sell"
    else:
        signal = "hold"

    channel_width = (channel_high - channel_low) / channel_low if channel_low > 0 else 0

    return SignalResult(
        signal,
        {
            "close": round(close, 4),
            "channel_high": round(channel_high, 4),
            "channel_low": round(channel_low, 4),
            "channel_width_pct": round(channel_width * 100, 2),
        },
        n,
        confidence=round(min(1.0, channel_width * 5), 4),
    )
