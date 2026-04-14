"""
Lag Correlation voter strategy.

Computes the rolling Pearson correlation between BTC close prices and the
target alt's close prices over the lookback window.

A high positive correlation is the baseline expectation. A *breakdown* in
that correlation (correlation drops sharply) signals a divergence opportunity:

  BUY  — correlation was high, BTC moved up, alt lagged → expect catch-up
  SELL — correlation was high, BTC moved down, alt lagged → expect catch-up

A supplementary direction filter (recent BTC vs alt direction) disambiguates
which side to trade when correlation breaks down.

Reads lag_features["btc_closes"] and ["target_closes"] injected by
lag_ensemble_core.
"""
from __future__ import annotations

import math
from typing import Any, Literal

from trading.strategies.base import SignalResult

_MIN_POINTS = 20  # minimum ticks needed for a meaningful correlation


def default_params() -> dict[str, Any]:
    return {
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        # Rolling window length for correlation (seconds).
        "corr_window": 30,
        # Correlation must have been above this before it breaks down.
        "baseline_corr": 0.80,
        # Correlation must fall below this to signal a breakdown.
        "breakdown_corr": 0.50,
    }


def _pearson(x: list[float], y: list[float]) -> float:
    """Return Pearson r for equal-length, non-empty lists. Returns 0.0 on error."""
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    denom_x = math.sqrt(sum((xi - mx) ** 2 for xi in x))
    denom_y = math.sqrt(sum((yi - my) ** 2 for yi in y))
    if denom_x == 0 or denom_y == 0:
        return 0.0
    return num / (denom_x * denom_y)


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    """Evaluate lag correlation breakdown signal."""
    lag_features: dict[str, Any] = params.get("lag_features") or {}
    btc_closes: list[float] = lag_features.get("btc_closes", [])
    tgt_closes: list[float] = lag_features.get("target_closes", [])
    tick_count: int = lag_features.get("tick_count", 0)

    close_count = len(ohlcv) if ohlcv else 0
    corr_window: int = int(params.get("corr_window", 30))

    if len(btc_closes) < max(_MIN_POINTS, corr_window):
        return SignalResult(
            signal="hold",
            meta={"reason": "warmup", "tick_count": tick_count},
            close_count=close_count,
            warmup=True,
        )

    # Full-window correlation (baseline) and recent-window correlation.
    baseline_window = min(len(btc_closes), corr_window * 2)
    full_corr = _pearson(
        btc_closes[-baseline_window:], tgt_closes[-baseline_window:]
    )
    recent_corr = _pearson(
        btc_closes[-corr_window:], tgt_closes[-corr_window:]
    )

    baseline_thresh: float = float(params.get("baseline_corr", 0.80))
    breakdown_thresh: float = float(params.get("breakdown_corr", 0.50))

    signal: Literal["buy", "sell", "hold"] = "hold"
    if full_corr >= baseline_thresh and recent_corr < breakdown_thresh:
        # Correlation broke down — determine direction from recent BTC move.
        btc_recent = btc_closes[-corr_window:]
        tgt_recent = tgt_closes[-corr_window:]
        btc_move = (
            (btc_recent[-1] - btc_recent[0]) / btc_recent[0]
            if btc_recent[0]
            else 0.0
        )
        tgt_move = (
            (tgt_recent[-1] - tgt_recent[0]) / tgt_recent[0]
            if tgt_recent[0]
            else 0.0
        )
        if btc_move > 0 and tgt_move < btc_move:
            signal = "buy"
        elif btc_move < 0 and tgt_move > btc_move:
            signal = "sell"

    confidence = (
        min(abs(full_corr - recent_corr), 1.0) if signal != "hold" else None
    )
    return SignalResult(
        signal=signal,
        meta={
            "full_corr": round(full_corr, 4),
            "recent_corr": round(recent_corr, 4),
            "corr_drop": round(full_corr - recent_corr, 4),
            "tick_count": tick_count,
        },
        close_count=close_count,
        warmup=False,
        confidence=confidence,
    )
