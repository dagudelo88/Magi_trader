"""
Bollinger Bands Breakout (Volatility Expansion) Strategy (#9)

Trades the "squeeze": when bands contract and then expand, price often makes
a strong directional move. A breakout above the upper band signals BUY;
below the lower band signals SELL.

BUY  — close breaks above upper BB after a squeeze (band_width < squeeze_threshold).
SELL — close breaks below lower BB after a squeeze.
HOLD — otherwise (no squeeze or no breakout).
"""
from __future__ import annotations

import math
from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _bb(closes: list[float], period: int, std_mult: float) -> tuple[float, float, float] | None:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    std = math.sqrt(sum((v - mid) ** 2 for v in window) / period)
    return mid + std_mult * std, mid, mid - std_mult * std


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "bb_period": 20,
        "bb_std": 2.0,
        "squeeze_threshold": 0.02,  # band_width / mid < this → squeeze is active
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    bb_period = int(params.get("bb_period", 20))
    bb_std = float(params.get("bb_std", 2.0))
    squeeze_thresh = float(params.get("squeeze_threshold", 0.02))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = bb_period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    # Current bands
    curr_bb = _bb(closes, bb_period, bb_std)
    # Previous bands (one bar ago) to detect the squeeze condition
    prev_bb = _bb(closes[:-1], bb_period, bb_std)

    if curr_bb is None or prev_bb is None:
        return SignalResult("hold", {}, n, warmup=True)

    upper, mid, lower = curr_bb
    prev_upper, prev_mid, prev_lower = prev_bb

    close = closes[-1]
    prev_close = closes[-2]

    # Band width as fraction of mid price
    prev_width = (prev_upper - prev_lower) / prev_mid if prev_mid > 0 else 0
    squeeze_was_active = prev_width < squeeze_thresh

    if squeeze_was_active and prev_close <= prev_upper and close > upper:
        signal = "buy"
    elif squeeze_was_active and prev_close >= prev_lower and close < lower:
        signal = "sell"
    else:
        signal = "hold"

    curr_width = (upper - lower) / mid if mid > 0 else 0

    return SignalResult(
        signal,
        {
            "close": round(close, 4),
            "bb_upper": round(upper, 4),
            "bb_mid": round(mid, 4),
            "bb_lower": round(lower, 4),
            "band_width": round(curr_width, 4),
            "prev_band_width": round(prev_width, 4),
            "squeeze_was_active": squeeze_was_active,
        },
        n,
    )
