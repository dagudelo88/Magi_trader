"""
Bollinger Bands + RSI Combo Strategy (#3)

Mean-reversion: buy near lower band when oversold; sell near upper band when overbought.

BUY  — close touches or crosses below the lower BB AND RSI < rsi_oversold.
SELL — close touches or crosses above the upper BB AND RSI > rsi_overbought.
HOLD — otherwise.
"""
from __future__ import annotations

import math
from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _std(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((v - mean) ** 2 for v in window) / period
    return math.sqrt(variance)


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
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "bb_period": 20,
        "bb_std": 2.0,
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
    bb_period = int(params.get("bb_period", 20))
    bb_std_mult = float(params.get("bb_std", 2.0))
    rsi_period = int(params.get("rsi_period", 14))
    rsi_ob = float(params.get("rsi_overbought", 70))
    rsi_os = float(params.get("rsi_oversold", 30))

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)
    min_bars = max(bb_period, rsi_period + 1)

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    mid = _sma(closes, bb_period)
    std = _std(closes, bb_period)
    rsi = _rsi(closes, rsi_period)

    if mid is None or std is None or rsi is None:
        return SignalResult("hold", {}, n, warmup=True)

    upper_band = mid + bb_std_mult * std
    lower_band = mid - bb_std_mult * std
    close = closes[-1]

    near_lower = close <= lower_band
    near_upper = close >= upper_band

    if near_lower and rsi < rsi_os:
        signal = "buy"
    elif near_upper and rsi > rsi_ob:
        signal = "sell"
    else:
        signal = "hold"

    band_width = (upper_band - lower_band) / mid if mid > 0 else 0
    return SignalResult(
        signal,
        {
            "close": round(close, 4),
            "bb_upper": round(upper_band, 4),
            "bb_mid": round(mid, 4),
            "bb_lower": round(lower_band, 4),
            "rsi": round(rsi, 2),
            "band_width": round(band_width, 4),
        },
        n,
        confidence=round(min(1.0, abs(close - mid) / (bb_std_mult * std + 1e-9)), 4) if std > 0 else None,
    )
