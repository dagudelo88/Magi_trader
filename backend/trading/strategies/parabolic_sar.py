"""
Parabolic SAR Trailing Reversal Strategy (#10)

The SAR dot flips position relative to price when a trend reversal occurs.

BUY  — SAR flips below price (trend turns bullish).
SELL — SAR flips above price (trend turns bearish).
HOLD — SAR remains on the same side as previous bar.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _compute_sar(
    highs: list[float],
    lows: list[float],
    step: float,
    max_step: float,
) -> list[float | None]:
    """
    Classic Parabolic SAR. Returns a series aligned with `highs`/`lows`.
    The first `step` bars are None during initialisation.
    """
    n = len(highs)
    if n < 2:
        return [None] * n

    sar: list[float | None] = [None] * n
    ep: float  # extreme point
    af: float  # acceleration factor

    # Initialise on bar 0 — assume uptrend to start
    bull = True
    sar[0] = lows[0]
    ep = highs[0]
    af = step

    for i in range(1, n):
        prev_sar = sar[i - 1]
        assert prev_sar is not None

        if bull:
            raw_sar = prev_sar + af * (ep - prev_sar)
            # SAR must not be above previous two lows
            raw_sar = min(raw_sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < raw_sar:
                # Flip to bearish
                bull = False
                sar[i] = ep
                ep = lows[i]
                af = step
            else:
                sar[i] = raw_sar
                if highs[i] > ep:
                    ep = highs[i]
                    af = min(af + step, max_step)
        else:
            raw_sar = prev_sar - af * (prev_sar - ep)
            # SAR must not be below previous two highs
            raw_sar = max(raw_sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > raw_sar:
                # Flip to bullish
                bull = True
                sar[i] = ep
                ep = highs[i]
                af = step
            else:
                sar[i] = raw_sar
                if lows[i] < ep:
                    ep = lows[i]
                    af = min(af + step, max_step)

    return sar


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "step": 0.02,
        "max_step": 0.2,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 80,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    step = float(params.get("step", 0.02))
    max_step = float(params.get("max_step", 0.2))

    highs = [float(x[2]) for x in ohlcv]
    lows = [float(x[3]) for x in ohlcv]
    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)

    if n < 3:
        return SignalResult("hold", {}, n, warmup=True)

    sar = _compute_sar(highs, lows, step, max_step)

    sar_now = sar[-1]
    sar_prev = sar[-2]
    close_now = closes[-1]
    close_prev = closes[-2]

    if sar_now is None or sar_prev is None:
        return SignalResult("hold", {}, n, warmup=True)

    was_above = sar_prev > close_prev  # SAR above price = bearish
    is_below = sar_now < close_now     # SAR below price = bullish

    if was_above and is_below:
        signal = "buy"
    elif not was_above and not is_below:
        signal = "sell"
    else:
        signal = "hold"

    return SignalResult(
        signal,
        {
            "sar": round(sar_now, 4),
            "close": round(close_now, 4),
            "prev_sar": round(sar_prev, 4),
            "sar_below_price": sar_now < close_now,
        },
        n,
    )
