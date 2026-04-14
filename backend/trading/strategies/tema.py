"""
Triple EMA (TEMA) Strategy (#12)

TEMA = 3*EMA1 − 3*EMA2 + EMA3  (reduces lag vs. a plain EMA).

BUY  — TEMA crosses above price (price breaks above TEMA from below).
SELL — TEMA crosses below price (price breaks below TEMA from above).
HOLD — no crossover.

Alternatively used as a trend filter: price above TEMA = bullish; below = bearish.
The signal here is the crossover moment only.
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


def _ema_series(values: list[float], period: int) -> list[float | None]:
    n = len(values)
    result: list[float | None] = [None] * n
    if n < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, n):
        result[i] = values[i] * k + result[i - 1] * (1 - k)  # type: ignore[operator]
    return result


def _tema(closes: list[float], period: int) -> float | None:
    """TEMA = 3*EMA(close) − 3*EMA(EMA(close)) + EMA(EMA(EMA(close)))"""
    ema1_series = _ema_series(closes, period)
    valid1 = [v for v in ema1_series if v is not None]
    if len(valid1) < period:
        return None

    ema2_series = _ema_series(valid1, period)
    valid2 = [v for v in ema2_series if v is not None]
    if len(valid2) < period:
        return None

    ema3_series = _ema_series(valid2, period)
    ema3_last = next((v for v in reversed(ema3_series) if v is not None), None)
    ema2_last = next((v for v in reversed(ema2_series) if v is not None), None)
    ema1_last = next((v for v in reversed(ema1_series) if v is not None), None)

    if None in (ema1_last, ema2_last, ema3_last):
        return None
    return 3 * ema1_last - 3 * ema2_last + ema3_last  # type: ignore[operator]


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "period": 20,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("period", 20))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = period * 3 + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    tema_now = _tema(closes, period)
    tema_prev = _tema(closes[:-1], period)

    if tema_now is None or tema_prev is None:
        return SignalResult("hold", {}, n, warmup=True)

    close_now = closes[-1]
    close_prev = closes[-2]

    was_above = close_prev > tema_prev
    is_above = close_now > tema_now

    if not was_above and is_above:
        signal = "buy"
    elif was_above and not is_above:
        signal = "sell"
    else:
        signal = "hold"

    gap = close_now - tema_now
    return SignalResult(
        signal,
        {
            "tema": round(tema_now, 4),
            "close": round(close_now, 4),
            "gap": round(gap, 4),
        },
        n,
        confidence=round(min(1.0, abs(gap) / (tema_now * 0.05 + 1e-9)), 4) if tema_now else None,
    )
