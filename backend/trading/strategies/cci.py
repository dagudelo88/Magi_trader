"""
CCI (Commodity Channel Index) Strategy (#13)

CCI measures deviation of price from its statistical mean.
Crosses above/below ±100 generate signals.

BUY  — CCI crosses above −100 (was below, now above).
SELL — CCI crosses below +100 (was above, now below).
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _typical_price(ohlcv_row: list) -> float:
    return (float(ohlcv_row[2]) + float(ohlcv_row[3]) + float(ohlcv_row[4])) / 3.0


def _cci(tp_series: list[float], period: int) -> float | None:
    if len(tp_series) < period:
        return None
    window = tp_series[-period:]
    mean = sum(window) / period
    mean_dev = sum(abs(v - mean) for v in window) / period
    if mean_dev == 0:
        return 0.0
    return (window[-1] - mean) / (0.015 * mean_dev)


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "period": 20,
        "upper_level": 100,
        "lower_level": -100,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 80,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("period", 20))
    upper = float(params.get("upper_level", 100))
    lower = float(params.get("lower_level", -100))

    tp_series = [_typical_price(row) for row in ohlcv]
    n = len(tp_series)
    min_bars = period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    cci_now = _cci(tp_series, period)
    cci_prev = _cci(tp_series[:-1], period)

    if cci_now is None or cci_prev is None:
        return SignalResult("hold", {}, n, warmup=True)

    if cci_prev <= lower and cci_now > lower:
        signal = "buy"
    elif cci_prev >= upper and cci_now < upper:
        signal = "sell"
    else:
        signal = "hold"

    return SignalResult(
        signal,
        {
            "cci": round(cci_now, 2),
            "prev_cci": round(cci_prev, 2),
            "upper_level": upper,
            "lower_level": lower,
        },
        n,
        confidence=round(min(1.0, abs(cci_now) / 200), 4),
    )
