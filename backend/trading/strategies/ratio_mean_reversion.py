"""
Ratio Mean Reversion voter strategy.

Tracks the BTC/alt price ratio (btc_price / target_price) and signals when it
deviates significantly from its rolling mean — an indication that the spread
between BTC and the alt has stretched and is likely to revert.

Signal logic:
  BUY  — ratio is significantly above its mean (alt is cheap relative to BTC;
         expect alt to catch up / ratio to fall by alt rising)
  SELL — ratio is significantly below its mean (alt is expensive relative to
         BTC; expect ratio to rise by alt falling)
  HOLD — ratio is within the normal band

Reads lag_features["btc_closes"] and ["target_closes"] injected by
lag_ensemble_core. Computes the ratio internally rather than using the
pre-computed latest_ratio to capture the full rolling window.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from trading.strategies.base import SignalResult

_MIN_POINTS = 20


def default_params() -> dict[str, Any]:
    return {
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        # Rolling window for mean/std of the BTC/alt ratio.
        "ratio_window": 40,
        # Signal when ratio deviates beyond this many standard deviations.
        "zscore_threshold": 1.8,
    }


def _rolling_zscore(series: list[float], window: int) -> float:
    """Return z-score of the latest value relative to the preceding window."""
    if len(series) < window + 1:
        return 0.0
    segment = series[-(window + 1):-1]  # window values before the latest
    latest = series[-1]
    mean = sum(segment) / len(segment)
    variance = sum((v - mean) ** 2 for v in segment) / len(segment)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (latest - mean) / std


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    """Evaluate BTC/alt ratio mean-reversion signal."""
    lag_features: dict[str, Any] = params.get("lag_features") or {}
    btc_closes: list[float] = lag_features.get("btc_closes", [])
    tgt_closes: list[float] = lag_features.get("target_closes", [])
    tick_count: int = lag_features.get("tick_count", 0)

    close_count = len(ohlcv) if ohlcv else 0
    ratio_window: int = int(params.get("ratio_window", 40))
    zscore_thresh: float = float(params.get("zscore_threshold", 1.8))

    n = min(len(btc_closes), len(tgt_closes))
    if n < max(_MIN_POINTS, ratio_window + 1):
        return SignalResult(
            signal="hold",
            meta={"reason": "warmup", "tick_count": tick_count},
            close_count=close_count,
            warmup=True,
        )

    ratios = [
        b / t if t != 0 else 1.0
        for b, t in zip(
            btc_closes[-ratio_window - 1:], tgt_closes[-ratio_window - 1:]
        )
    ]

    zscore = _rolling_zscore(ratios, ratio_window)

    signal: Literal["buy", "sell", "hold"]
    if zscore > zscore_thresh:
        # Ratio above mean → alt is cheap → expect catch-up → BUY
        signal = "buy"
    elif zscore < -zscore_thresh:
        # Ratio below mean → alt is expensive → expect reversion → SELL
        signal = "sell"
    else:
        signal = "hold"

    confidence = (
        min(abs(zscore) / (zscore_thresh * 2), 1.0) if signal != "hold" else None
    )
    return SignalResult(
        signal=signal,
        meta={
            "ratio_zscore": round(zscore, 4),
            "latest_ratio": round(ratios[-1], 6) if ratios else 0.0,
            "tick_count": tick_count,
        },
        close_count=close_count,
        warmup=False,
        confidence=confidence,
    )
