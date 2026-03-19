from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 1:
        return None
    return sum(values[-period:]) / period


@dataclass(frozen=True)
class SignalDetails:
    """Latest crossover evaluation on close prices (for logging / UI)."""

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
    Same rules as evaluate_signal, plus SMA values for debugging.
    Buy when fast crosses above slow; sell when fast crosses below slow.
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


def default_strategy_params() -> dict[str, Any]:
    return {
        "fast_period": 5,
        "slow_period": 15,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 50,
    }
