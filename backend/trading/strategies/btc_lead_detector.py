"""
BTC Lead Detector voter strategy.

Detects when BTC is leading (making a strong directional move) while the
target altcoin is lagging — expecting the alt to catch up shortly.

Signal logic:
  BUY  — BTC average 1s ROC over the last N ticks is strongly positive AND
          the alt's ROC is still flat/negative (BTC leading up, alt lagging).
  SELL — BTC average 1s ROC is strongly negative AND the alt's ROC is still
         flat/positive (BTC leading down, alt lagging).
  HOLD — momentum delta is below the threshold or both assets are moving
         together.

Reads lag_features["btc_roc_1s"] and ["target_roc_1s"] injected by
lag_ensemble_core — no direct DB access.
"""
from __future__ import annotations

from typing import Any, Literal

from trading.strategies.base import SignalResult

# Number of the most-recent ticks to average for momentum estimation.
_WINDOW = 10


def default_params() -> dict[str, Any]:
    return {
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        # Minimum |BTC momentum| to consider it a leading move.
        # Calibrated to typical 10-tick BTC 1s ROC observed in market_ticks
        # (~0.00005–0.0003 range during normal sessions).
        "btc_momentum_threshold": 0.00006,
        # Maximum |alt momentum| to confirm the alt is still lagging.
        "alt_lag_threshold": 0.00003,
        # Number of recent ticks to average (overridable per bot).
        "momentum_window": _WINDOW,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    """Evaluate BTC-lead / alt-lag signal from injected lag_features."""
    lag_features: dict[str, Any] = params.get("lag_features") or {}
    btc_roc: list[float] = lag_features.get("btc_roc_1s", [])
    target_roc: list[float] = lag_features.get("target_roc_1s", [])
    tick_count: int = lag_features.get("tick_count", 0)

    close_count = len(ohlcv) if ohlcv else 0

    # Warmup: need at least momentum_window ticks of lag data.
    window: int = int(params.get("momentum_window", _WINDOW))
    if len(btc_roc) < window or len(target_roc) < window:
        return SignalResult(
            signal="hold",
            meta={"reason": "warmup", "tick_count": tick_count},
            close_count=close_count,
            warmup=True,
        )

    btc_slice = btc_roc[-window:]
    tgt_slice = target_roc[-window:]

    btc_momentum = sum(btc_slice) / window
    alt_momentum = sum(tgt_slice) / window

    btc_thresh: float = float(params.get("btc_momentum_threshold", 0.0008))
    alt_lag_thresh: float = float(params.get("alt_lag_threshold", 0.0002))

    signal: Literal["buy", "sell", "hold"]
    if btc_momentum > btc_thresh and abs(alt_momentum) < alt_lag_thresh:
        signal = "buy"
    elif btc_momentum < -btc_thresh and abs(alt_momentum) < alt_lag_thresh:
        signal = "sell"
    else:
        signal = "hold"

    lag_score = round(btc_momentum - alt_momentum, 6)
    confidence = (
        min(abs(lag_score) / (btc_thresh * 2), 1.0)
        if signal != "hold"
        else None
    )
    return SignalResult(
        signal=signal,
        meta={
            "btc_momentum": round(btc_momentum, 6),
            "alt_momentum": round(alt_momentum, 6),
            "lag_score": lag_score,
            "tick_count": tick_count,
        },
        close_count=close_count,
        warmup=False,
        confidence=confidence,
    )
