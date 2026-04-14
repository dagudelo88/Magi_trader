"""
Stochastic Oscillator (%K / %D Cross) Strategy (#8)

%K/%D momentum cross inside overbought/oversold zones.

BUY  — %K crosses above %D while both are below the oversold threshold (e.g. 20).
SELL — %K crosses below %D while both are above the overbought threshold (e.g. 80).
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _stoch_k(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    n = len(closes)
    result: list[float | None] = [None] * n
    for i in range(period - 1, n):
        h = max(highs[i - period + 1: i + 1])
        l = min(lows[i - period + 1: i + 1])
        result[i] = (closes[i] - l) / (h - l) * 100 if h != l else 50.0
    return result


def _sma_of(values: list[float | None], period: int) -> list[float | None]:
    n = len(values)
    result: list[float | None] = [None] * n
    for i in range(n):
        window = [v for v in values[max(0, i - period + 1): i + 1] if v is not None]
        if len(window) == period:
            result[i] = sum(window) / period
    return result


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "k_period": 14,
        "d_period": 3,
        "overbought": 80,
        "oversold": 20,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    k_period = int(params.get("k_period", 14))
    d_period = int(params.get("d_period", 3))
    ob = float(params.get("overbought", 80))
    os_ = float(params.get("oversold", 20))

    highs = [float(x[2]) for x in ohlcv]
    lows = [float(x[3]) for x in ohlcv]
    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = k_period + d_period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    k_series = _stoch_k(highs, lows, closes, k_period)
    d_series = _sma_of(k_series, d_period)

    k_now = k_series[-1]
    k_prev = k_series[-2]
    d_now = d_series[-1]
    d_prev = d_series[-2]

    if None in (k_now, k_prev, d_now, d_prev):
        return SignalResult("hold", {}, n, warmup=True)

    # Buy: %K crosses above %D from oversold zone
    if k_prev <= d_prev and k_now > d_now and k_now < ob:  # type: ignore[operator]
        signal = "buy"
    # Sell: %K crosses below %D from overbought zone
    elif k_prev >= d_prev and k_now < d_now and k_now > os_:  # type: ignore[operator]
        signal = "sell"
    else:
        signal = "hold"

    return SignalResult(
        signal,
        {
            "k": round(k_now, 2),  # type: ignore[arg-type]
            "d": round(d_now, 2),  # type: ignore[arg-type]
            "prev_k": round(k_prev, 2),  # type: ignore[arg-type]
            "prev_d": round(d_prev, 2),  # type: ignore[arg-type]
        },
        n,
    )
