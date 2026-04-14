"""
EMA Ribbon (Multi-EMA Alignment) Strategy (#6)

8–12 EMAs stacked in ascending order = confirmed uptrend; descending = downtrend.

BUY  — all EMA periods align in ascending order (short > long) AND close > fastest EMA.
SELL — all EMA periods align in descending order (short < long) AND close < fastest EMA.
HOLD — mixed alignment.
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
        "ema_periods": [8, 13, 21, 34, 55],
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 150,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    periods: list[int] = [int(p) for p in params.get("ema_periods", [8, 13, 21, 34, 55])]
    periods = sorted(set(periods))  # ensure unique, ascending

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = max(periods) + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    ema_vals = {}
    for p in periods:
        v = _ema(closes, p)
        if v is None:
            return SignalResult("hold", {}, n, warmup=True)
        ema_vals[p] = v

    sorted_periods = sorted(periods)
    values_asc = [ema_vals[p] for p in sorted_periods]

    # Bullish: each shorter EMA > next longer EMA (all stacked upward)
    bullish = all(values_asc[i] > values_asc[i + 1] for i in range(len(values_asc) - 1))
    # Bearish: each shorter EMA < next longer EMA (all stacked downward)
    bearish = all(values_asc[i] < values_asc[i + 1] for i in range(len(values_asc) - 1))

    close = closes[-1]
    fastest_ema = ema_vals[sorted_periods[0]]

    if bullish and close > fastest_ema:
        signal = "buy"
    elif bearish and close < fastest_ema:
        signal = "sell"
    else:
        signal = "hold"

    # Alignment ratio: fraction of consecutive pairs in the dominant order
    pairs = len(values_asc) - 1
    bull_pairs = sum(1 for i in range(pairs) if values_asc[i] > values_asc[i + 1])
    alignment = bull_pairs / pairs if pairs > 0 else 0.5
    confidence = round(abs(alignment - 0.5) * 2, 4)  # 0 = mixed, 1 = full alignment

    meta = {f"ema_{p}": round(ema_vals[p], 4) for p in sorted_periods}
    meta["close"] = round(close, 4)
    meta["alignment"] = round(alignment, 4)

    return SignalResult(signal, meta, n, confidence=confidence)
