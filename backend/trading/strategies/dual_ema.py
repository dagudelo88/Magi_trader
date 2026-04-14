"""
Dual EMA Crossover Strategy (#7)

Faster-reacting alternative to SMA cross using exponential moving averages.

BUY  — fast EMA crosses above slow EMA.
SELL — fast EMA crosses below slow EMA.
HOLD — no crossover this bar.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "fast": 9,
        "slow": 21,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 80,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    fast = int(params.get("fast", 9))
    slow = int(params.get("slow", 21))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = slow + 2

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    prev_closes = closes[:-1]
    fast_now = _ema(closes, fast)
    slow_now = _ema(closes, slow)
    fast_prev = _ema(prev_closes, fast)
    slow_prev = _ema(prev_closes, slow)

    if None in (fast_now, slow_now, fast_prev, slow_prev):
        return SignalResult("hold", {}, n, warmup=True)

    if fast_prev <= slow_prev and fast_now > slow_now:  # type: ignore[operator]
        signal = "buy"
    elif fast_prev >= slow_prev and fast_now < slow_now:  # type: ignore[operator]
        signal = "sell"
    else:
        signal = "hold"

    gap = fast_now - slow_now  # type: ignore[operator]
    confidence = round(min(1.0, abs(gap) / (slow_now * 0.05 + 1e-9)), 4) if slow_now else None  # type: ignore[operator]

    return SignalResult(
        signal,
        {
            "fast_ema": round(fast_now, 4),  # type: ignore[arg-type]
            "slow_ema": round(slow_now, 4),  # type: ignore[arg-type]
            "gap": round(gap, 4),
        },
        n,
        confidence=confidence,
    )
