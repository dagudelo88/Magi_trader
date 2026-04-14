"""
Supertrend Trend-Following Strategy (#2)

Uses ATR-based trailing bands. The Supertrend line flips direction when price
closes beyond the upper (bearish) or lower (bullish) band.

BUY  — Supertrend flips from bearish (above price) to bullish (below price).
SELL — Supertrend flips from bullish to bearish.
HOLD — No directional flip this bar.
"""
from __future__ import annotations

from typing import Any

from trading.strategies.base import SignalResult


# ── helpers ──────────────────────────────────────────────────────────────────

def _true_ranges(highs: list[float], lows: list[float], closes: list[float]) -> list[float]:
    trs = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        prev_c = closes[i - 1] if i > 0 else closes[i]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return trs


def _wilder_atr(trs: list[float], period: int) -> list[float | None]:
    """Wilder's smoothed ATR series (same length as trs, None until warm)."""
    n = len(trs)
    result: list[float | None] = [None] * n
    if n < period:
        return result
    result[period - 1] = sum(trs[:period]) / period
    for i in range(period, n):
        result[i] = (result[i - 1] * (period - 1) + trs[i]) / period  # type: ignore[operator]
    return result


# ── public interface ──────────────────────────────────────────────────────────

def default_params() -> dict[str, Any]:
    return {
        "period": 10,
        "multiplier": 3.0,
        "quote_fraction": 0.02,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 100,
        "initial_budget_quote": None,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    period = int(params.get("period", 10))
    multiplier = float(params.get("multiplier", 3.0))

    highs = [float(x[2]) for x in ohlcv]
    lows = [float(x[3]) for x in ohlcv]
    closes = [float(x[4]) for x in ohlcv]
    n = len(closes)

    min_bars = period + 2
    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    trs = _true_ranges(highs, lows, closes)
    atr_series = _wilder_atr(trs, period)

    # ── build band + direction arrays ────────────────────────────────────────
    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    direction: list[int] = [0] * n  # 1=bullish, -1=bearish

    start = period - 1
    for i in range(start, n):
        atr = atr_series[i]
        if atr is None:
            continue
        mid = (highs[i] + lows[i]) / 2.0
        bu = mid + multiplier * atr
        bl = mid - multiplier * atr

        if i == start:
            upper[i] = bu
            lower[i] = bl
            direction[i] = 1 if closes[i] >= bl else -1
            continue

        prev_upper = upper[i - 1]
        prev_lower = lower[i - 1]

        # Bands only tighten to prevent premature flips
        upper[i] = bu if (bu < prev_upper or closes[i - 1] > prev_upper) else prev_upper  # type: ignore[operator]
        lower[i] = bl if (bl > prev_lower or closes[i - 1] < prev_lower) else prev_lower  # type: ignore[operator]

        prev_dir = direction[i - 1]
        if prev_dir == -1:
            direction[i] = 1 if closes[i] > upper[i] else -1  # type: ignore[operator]
        else:
            direction[i] = -1 if closes[i] < lower[i] else 1  # type: ignore[operator]

    curr_dir = direction[-1]
    prev_dir = direction[-2]

    if curr_dir == 0 or prev_dir == 0:
        return SignalResult("hold", {}, n, warmup=True)

    if curr_dir == 1 and prev_dir == -1:
        signal = "buy"
    elif curr_dir == -1 and prev_dir == 1:
        signal = "sell"
    else:
        signal = "hold"

    st_value = lower[-1] if curr_dir == 1 else upper[-1]
    atr_val = atr_series[-1]

    return SignalResult(
        signal,
        {
            "supertrend": round(st_value, 4) if st_value is not None else None,
            "close": round(closes[-1], 4),
            "atr": round(atr_val, 6) if atr_val is not None else None,
            "direction": "bullish" if curr_dir == 1 else "bearish",
        },
        n,
        confidence=round(min(1.0, (atr_val / closes[-1]) * multiplier * 10), 4) if atr_val else None,
    )
