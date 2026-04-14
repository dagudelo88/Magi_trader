from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from trading.strategies.base import SignalResult


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 1:
        return None
    return sum(values[-period:]) / period


@dataclass(frozen=True)
class SignalDetails:
    """Legacy crossover evaluation — kept for backward compatibility."""

    signal: Literal["buy", "sell", "hold"]
    fast_sma: float | None
    slow_sma: float | None
    prev_fast_sma: float | None
    prev_slow_sma: float | None
    close_count: int


def evaluate_signal_details(
    closes: list[float],
    fast_period: int,
    slow_period: int,
) -> SignalDetails:
    """
    Buy when fast SMA crosses above slow SMA; sell when crosses below.
    Returns SMA values for logging.
    """
    n = len(closes)
    if n < slow_period + 2:
        return SignalDetails("hold", None, None, None, None, n)

    prev_closes = closes[:-1]
    f_prev = _sma(prev_closes, fast_period)
    s_prev = _sma(prev_closes, slow_period)
    f_now = _sma(closes, fast_period)
    s_now = _sma(closes, slow_period)

    if f_prev is None or s_prev is None or f_now is None or s_now is None:
        return SignalDetails("hold", f_now, s_now, f_prev, s_prev, n)

    if f_prev <= s_prev and f_now > s_now:
        return SignalDetails("buy", f_now, s_now, f_prev, s_prev, n)
    if f_prev >= s_prev and f_now < s_now:
        return SignalDetails("sell", f_now, s_now, f_prev, s_prev, n)
    return SignalDetails("hold", f_now, s_now, f_prev, s_prev, n)


def evaluate_signal(
    closes: list[float],
    fast_period: int,
    slow_period: int,
) -> Literal["buy", "sell", "hold"]:
    return evaluate_signal_details(closes, fast_period, slow_period).signal


# ── Registry-compatible interface ─────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "fast_period": 5,
        "slow_period": 15,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 50,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    """Standard evaluate() used by the registry-based bot runner."""
    closes = [float(x[4]) for x in ohlcv]
    fast = int(params.get("fast_period", 5))
    slow = int(params.get("slow_period", 15))
    details = evaluate_signal_details(closes, fast, slow)

    warmup = details.fast_sma is None or details.slow_sma is None
    confidence: float | None = None
    if details.slow_sma is not None and details.slow_sma > 0 and details.fast_sma is not None:
        confidence = round(
            min(1.0, abs(details.fast_sma - details.slow_sma) / details.slow_sma * 20), 4
        )

    return SignalResult(
        details.signal,
        {
            "fast_sma": round(details.fast_sma, 4) if details.fast_sma is not None else None,
            "slow_sma": round(details.slow_sma, 4) if details.slow_sma is not None else None,
            "prev_fast_sma": round(details.prev_fast_sma, 4) if details.prev_fast_sma is not None else None,
            "prev_slow_sma": round(details.prev_slow_sma, 4) if details.prev_slow_sma is not None else None,
            "gap": round(details.fast_sma - details.slow_sma, 4)
            if details.fast_sma is not None and details.slow_sma is not None
            else None,
        },
        close_count=details.close_count,
        warmup=warmup,
        confidence=confidence,
    )


# Legacy alias kept for code that imports default_strategy_params directly.
default_strategy_params = default_params
