"""
ROC Divergence voter strategy.

Computes the divergence between BTC 5-second rate-of-change and the target
alt's 5-second rate-of-change. A large positive divergence (BTC moved more
than alt) implies a catch-up opportunity; a large negative divergence implies
the alt ran ahead and may revert.

Signal logic:
  BUY  — BTC 5s ROC mean significantly exceeds alt 5s ROC mean (alt lagging up)
  SELL — alt 5s ROC mean significantly exceeds BTC 5s ROC mean (alt leading,
         likely to revert, or alt running ahead while BTC turns down)
  HOLD — divergence within noise band

Reads lag_features["btc_roc_5s"] and ["target_roc_5s"] injected by
lag_ensemble_core.
"""
from __future__ import annotations

from typing import Any, Literal

from trading.strategies.base import SignalResult

_WINDOW = 12  # number of 1-second ticks for 5s-ROC average (~1 min of data)


def default_params() -> dict[str, Any]:
    return {
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        # |BTC 5s ROC - alt 5s ROC| must exceed this to generate a signal.
        # Calibrated to typical 5s ROC spread observed in market_ticks
        # (~0.0001–0.0005 during normal sessions).
        "divergence_threshold": 0.00008,
        "momentum_window": _WINDOW,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    """Evaluate ROC divergence signal between BTC and alt."""
    lag_features: dict[str, Any] = params.get("lag_features") or {}
    btc_roc5: list[float] = lag_features.get("btc_roc_5s", [])
    tgt_roc5: list[float] = lag_features.get("target_roc_5s", [])
    tick_count: int = lag_features.get("tick_count", 0)

    close_count = len(ohlcv) if ohlcv else 0
    window: int = int(params.get("momentum_window", _WINDOW))

    if len(btc_roc5) < window or len(tgt_roc5) < window:
        return SignalResult(
            signal="hold",
            meta={"reason": "warmup", "tick_count": tick_count},
            close_count=close_count,
            warmup=True,
        )

    btc_mean = sum(btc_roc5[-window:]) / window
    tgt_mean = sum(tgt_roc5[-window:]) / window
    divergence = btc_mean - tgt_mean

    thresh: float = float(params.get("divergence_threshold", 0.0015))

    signal: Literal["buy", "sell", "hold"]
    if divergence > thresh:
        signal = "buy"
    elif divergence < -thresh:
        signal = "sell"
    else:
        signal = "hold"

    confidence = (
        min(abs(divergence) / (thresh * 2), 1.0) if signal != "hold" else None
    )
    return SignalResult(
        signal=signal,
        meta={
            "btc_roc5_mean": round(btc_mean, 6),
            "tgt_roc5_mean": round(tgt_mean, 6),
            "divergence": round(divergence, 6),
            "tick_count": tick_count,
        },
        close_count=close_count,
        warmup=False,
        confidence=confidence,
    )
