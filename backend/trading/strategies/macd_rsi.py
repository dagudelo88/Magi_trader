"""
MACD Crossover with RSI Filter Strategy (#4)

Momentum: MACD line/signal cross confirmed by RSI to filter whipsaws.

BUY  — MACD line crosses above signal line AND RSI > 50.
SELL — MACD line crosses below signal line AND RSI < 50.
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "fast": 12,
        "slow": 26,
        "signal": 9,
        "rsi_period": 14,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    fast = int(params.get("fast", 12))
    slow = int(params.get("slow", 26))
    sig_period = int(params.get("signal", 9))
    rsi_period = int(params.get("rsi_period", 14))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = slow + sig_period + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)

    # MACD line = fast EMA − slow EMA (only where both are valid)
    macd_line: list[float | None] = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    # Signal line = EMA(macd_line, sig_period) — only over valid MACD values
    valid_macd = [(i, v) for i, v in enumerate(macd_line) if v is not None]
    if len(valid_macd) < sig_period + 1:
        return SignalResult("hold", {}, n, warmup=True)

    # Build a compact list of valid MACD values for EMA calc
    macd_vals = [v for _, v in valid_macd]
    sig_ema = _ema_series(macd_vals, sig_period)

    # Align back: last values
    macd_now = macd_vals[-1]
    macd_prev = macd_vals[-2]
    sig_now = sig_ema[-1]
    sig_prev = sig_ema[-2]

    if sig_now is None or sig_prev is None:
        return SignalResult("hold", {}, n, warmup=True)

    rsi = _rsi(closes, rsi_period)
    if rsi is None:
        return SignalResult("hold", {}, n, warmup=True)

    histogram = macd_now - sig_now
    if macd_prev <= sig_prev and macd_now > sig_now and rsi > 50:
        signal = "buy"
    elif macd_prev >= sig_prev and macd_now < sig_now and rsi < 50:
        signal = "sell"
    else:
        signal = "hold"

    return SignalResult(
        signal,
        {
            "macd": round(macd_now, 6),
            "signal_line": round(sig_now, 6),
            "histogram": round(histogram, 6),
            "rsi": round(rsi, 2),
        },
        n,
        confidence=round(min(1.0, abs(histogram) / (abs(macd_now) + 1e-9)), 4),
    )
