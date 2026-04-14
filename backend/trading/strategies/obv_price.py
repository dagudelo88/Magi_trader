"""
On-Balance Volume (OBV) + Price Confirmation Strategy (#14)

Volume-weighted momentum: OBV rising + price breakout confirms real buying pressure.

BUY  — OBV trend is up (OBV SMA rising) AND price breaks above its short-term SMA.
SELL — OBV trend is down (OBV SMA falling) AND price falls below its short-term SMA.
HOLD — divergence between OBV and price, or insufficient data.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _obv_series(closes: list[float], volumes: list[float]) -> list[float]:
    obv = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])
    return obv


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "obv_period": 20,
        "price_period": 10,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    obv_period = int(params.get("obv_period", 20))
    price_period = int(params.get("price_period", 10))

    closes = [float(x[4]) for x in ohlcv]
    volumes = [float(x[5]) for x in ohlcv]
    n = len(closes)
    min_bars = max(obv_period, price_period) + 1

    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    obv = _obv_series(closes, volumes)

    obv_sma_now = _sma(obv, obv_period)
    obv_sma_prev = _sma(obv[:-1], obv_period)
    price_sma = _sma(closes, price_period)

    if None in (obv_sma_now, obv_sma_prev, price_sma):
        return SignalResult("hold", {}, n, warmup=True)

    close = closes[-1]
    obv_rising = obv_sma_now > obv_sma_prev  # type: ignore[operator]
    price_above_sma = close > price_sma  # type: ignore[operator]

    if obv_rising and price_above_sma:
        signal = "buy"
    elif not obv_rising and not price_above_sma:
        signal = "sell"
    else:
        signal = "hold"

    obv_momentum = obv_sma_now - obv_sma_prev  # type: ignore[operator]

    return SignalResult(
        signal,
        {
            "obv": round(obv[-1], 2),
            "obv_sma": round(obv_sma_now, 2),  # type: ignore[arg-type]
            "price_sma": round(price_sma, 4),  # type: ignore[arg-type]
            "close": round(close, 4),
            "obv_rising": obv_rising,
        },
        n,
        confidence=round(min(1.0, abs(obv_momentum) / (abs(obv_sma_now) + 1e-9)), 4) if obv_sma_now else None,  # type: ignore[arg-type]
    )
