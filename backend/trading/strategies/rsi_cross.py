"""
RSI Mean-Reversion with Explicit Crossover Strategy (#5)

Triggers on the crossover moment only — avoids repeated signals while RSI
remains in the overbought/oversold zone.

BUY  — RSI crosses ABOVE the oversold threshold (e.g. rises from below 30 to above 30).
SELL — RSI crosses BELOW the overbought threshold (e.g. falls from above 70 to below 70).
HOLD — otherwise.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _rsi_at(closes: list[float], period: int) -> float | None:
    """Compute RSI for the last bar using a simple avg-gain/avg-loss window."""
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
        "rsi_period": 14,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("rsi_period", 14))
    ob = float(params.get("rsi_overbought", 70))
    os_ = float(params.get("rsi_oversold", 30))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = period + 2

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    rsi_now = _rsi_at(closes, period)
    rsi_prev = _rsi_at(closes[:-1], period)

    if rsi_now is None or rsi_prev is None:
        return SignalResult("hold", {}, n, warmup=True)

    if rsi_prev <= os_ and rsi_now > os_:
        signal = "buy"
    elif rsi_prev >= ob and rsi_now < ob:
        signal = "sell"
    else:
        signal = "hold"

    # Distance from the neutral 50 level normalised to 0–1
    confidence = round(min(1.0, abs(rsi_now - 50) / 50), 4)

    return SignalResult(
        signal,
        {
            "rsi": round(rsi_now, 2),
            "prev_rsi": round(rsi_prev, 2),
            "overbought": ob,
            "oversold": os_,
        },
        n,
        confidence=confidence,
    )
