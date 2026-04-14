"""
Spot Grid Trading Strategy (#1)

Places virtual grid levels inside a price range. Detects when price crosses a
grid boundary and signals accordingly — pure mean-reversion on oscillation.

BUY  — price moved down to a lower grid zone vs. previous bar.
SELL — price moved up to a higher grid zone vs. previous bar.
HOLD — price stayed within the same grid zone.

The price range is auto-computed from the OHLCV window when range_low/range_high
are not explicitly provided.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


def default_params() -> dict[str, Any]:
    return {
        "grid_levels": 20,
        "grid_spacing_percent": 1.0,  # used only if range_low/range_high are None
        "range_low": None,            # explicit lower bound; None = auto from window
        "range_high": None,           # explicit upper bound; None = auto from window
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 60,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 200,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    grid_levels = int(params.get("grid_levels", 20))
    range_low_cfg = params.get("range_low")
    range_high_cfg = params.get("range_high")

    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)

    if n < 2:
        return SignalResult("hold", {}, n, warmup=True)

    range_low = float(range_low_cfg) if range_low_cfg is not None else min(closes)
    range_high = float(range_high_cfg) if range_high_cfg is not None else max(closes)

    if range_high <= range_low or grid_levels < 2:
        return SignalResult(
            "hold",
            {"error": "invalid_range", "range_low": range_low, "range_high": range_high},
            n,
        )

    step = (range_high - range_low) / grid_levels

    def _zone(price: float) -> int:
        """Grid zone index (0 = at/below range_low, grid_levels = at/above range_high)."""
        return int((price - range_low) / step)

    prev_zone = _zone(closes[-2])
    curr_zone = _zone(closes[-1])
    close = closes[-1]

    if curr_zone < prev_zone:
        signal = "buy"
    elif curr_zone > prev_zone:
        signal = "sell"
    else:
        signal = "hold"

    # Position within range as a 0–1 fraction
    range_position = (close - range_low) / (range_high - range_low)

    return SignalResult(
        signal,
        {
            "close": round(close, 4),
            "curr_zone": curr_zone,
            "prev_zone": prev_zone,
            "range_low": round(range_low, 4),
            "range_high": round(range_high, 4),
            "grid_step": round(step, 4),
            "range_position_pct": round(range_position * 100, 1),
        },
        n,
    )
