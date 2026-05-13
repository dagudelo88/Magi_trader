"""
Fixed Profit Rinse Repeat Strategy

Pyramiding spot strategy that tracks each filled buy independently and only
sells a specific entry after that entry reaches its configured profit target.
"""
from __future__ import annotations

import time
from typing import Any

from database import get_db_connection
from trading.strategies.base import SignalResult


_EPS = 1e-12


def _f(x: Any, default: float = 0.0) -> float:
    try:
        value = float(x)
    except (TypeError, ValueError):
        return default
    return value if value == value else default


def _ema(values: list[float], period: int) -> float | None:
    if period < 1 or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * k + result * (1.0 - k)
    return result


def _ema_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period < 1 or len(values) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = sum(values[:period]) / period
    for i in range(period, len(values)):
        result[i] = (
            values[i] * k + result[i - 1] * (1.0 - k)  # type: ignore[operator]
        )
    return result


def _sma(values: list[float], period: int) -> float | None:
    if period < 1 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def _rsi(closes: list[float], period: int) -> float | None:
    if period < 1 or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss <= 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _true_ranges(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> list[float]:
    ranges: list[float] = []
    for i, (high, low) in enumerate(zip(highs, lows)):
        prev_close = closes[i - 1] if i > 0 else closes[i]
        ranges.append(
            max(high - low, abs(high - prev_close), abs(low - prev_close))
        )
    return ranges


def _wilder_atr(ranges: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(ranges)
    if period < 1 or len(ranges) < period:
        return result
    result[period - 1] = sum(ranges[:period]) / period
    for i in range(period, len(ranges)):
        prev_atr = result[i - 1]
        if prev_atr is None:
            continue
        result[i] = (
            prev_atr * (period - 1) + ranges[i]
        ) / period
    return result


def _supertrend_bullish(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
    multiplier: float,
) -> tuple[bool | None, float | None]:
    ranges = _true_ranges(highs, lows, closes)
    atr = _wilder_atr(ranges, period)
    n = len(closes)
    if n < period + 2:
        return None, None

    upper: list[float | None] = [None] * n
    lower: list[float | None] = [None] * n
    direction: list[int] = [0] * n
    start = period - 1
    for i in range(start, n):
        atr_value = atr[i]
        if atr_value is None:
            continue
        mid = (highs[i] + lows[i]) / 2.0
        basic_upper = mid + multiplier * atr_value
        basic_lower = mid - multiplier * atr_value
        if i == start:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = 1 if closes[i] >= basic_lower else -1
            continue

        prev_upper = upper[i - 1]
        prev_lower = lower[i - 1]
        if prev_upper is None or prev_lower is None:
            upper[i] = basic_upper
            lower[i] = basic_lower
            direction[i] = direction[i - 1]
            continue

        upper[i] = (
            basic_upper
            if basic_upper < prev_upper or closes[i - 1] > prev_upper
            else prev_upper
        )
        lower[i] = (
            basic_lower
            if basic_lower > prev_lower or closes[i - 1] < prev_lower
            else prev_lower
        )
        if direction[i - 1] == -1:
            direction[i] = (
                1 if closes[i] > upper[i] else -1  # type: ignore[operator]
            )
        else:
            direction[i] = (
                -1 if closes[i] < lower[i] else 1  # type: ignore[operator]
            )

    current_direction = direction[-1]
    if current_direction == 0:
        return None, atr[-1]
    return current_direction == 1, atr[-1]


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


def _load_open_entries(
    bot_id: str,
    execution_mode: str,
    symbol: str,
) -> list[dict[str, Any]]:
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT entry_id, bot_id, execution_mode, symbol,
                   entry_price, quantity,
                   exchange_order_id, created_at
            FROM strategy_open_entries
            WHERE bot_id = ?
              AND execution_mode = ?
              AND symbol = ?
              AND quantity > ?
            ORDER BY created_at ASC, entry_id ASC
            """,
            (bot_id, execution_mode, symbol, _EPS),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _total_open_cost(entries: list[dict[str, Any]]) -> float:
    return sum(
        _f(entry.get("entry_price")) * _f(entry.get("quantity"))
        for entry in entries
    )


def _vote_to_signal(value: bool | None) -> str:
    return "buy" if value is True else "hold"


def _directional_votes(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    volumes: list[float],
    params: dict[str, Any],
) -> tuple[dict[str, bool | None], dict[str, float | None]]:
    st_period = int(params.get("supertrend_period", 10))
    st_multiplier = float(params.get("supertrend_multiplier", 3.0))
    supertrend_ok, atr_value = _supertrend_bullish(
        highs,
        lows,
        closes,
        st_period,
        st_multiplier,
    )

    ema_periods = [
        int(p)
        for p in params.get("ema_periods", [8, 13, 21, 34, 55])
    ]
    ema_periods = sorted(set(p for p in ema_periods if p > 0))
    ema_values = {period: _ema(closes, period) for period in ema_periods}
    ema_ready = bool(ema_values) and all(
        value is not None for value in ema_values.values()
    )
    ema_ribbon_ok = None
    if ema_ready:
        ordered = [
            value
            for p in ema_periods
            if (value := ema_values[p]) is not None
        ]
        ribbon_pairs = range(len(ordered) - 1)
        ema_ribbon_ok = all(
            ordered[i] > ordered[i + 1] for i in ribbon_pairs
        ) and closes[-1] > ordered[0]

    fast_period = int(params.get("dual_ema_fast", 9))
    slow_period = int(params.get("dual_ema_slow", 21))
    fast_ema = _ema(closes, fast_period)
    slow_ema = _ema(closes, slow_period)
    dual_ema_ok = (
        fast_ema is not None
        and slow_ema is not None
        and fast_ema > slow_ema
    )
    ema_alignment_ok = bool(ema_ribbon_ok or dual_ema_ok)

    macd_fast = int(params.get("macd_fast", 12))
    macd_slow = int(params.get("macd_slow", 26))
    macd_signal_period = int(params.get("macd_signal", 9))
    fast_series = _ema_series(closes, macd_fast)
    slow_series = _ema_series(closes, macd_slow)
    macd_values = [
        fast - slow
        for fast, slow in zip(fast_series, slow_series)
        if fast is not None and slow is not None
    ]
    signal_series = _ema_series(macd_values, macd_signal_period)
    macd_now = macd_values[-1] if macd_values else None
    macd_signal = signal_series[-1] if signal_series else None
    rsi_period = int(params.get("rsi_period", 14))
    rsi_value = _rsi(closes, rsi_period)
    macd_rsi_ok = (
        macd_now is not None
        and macd_signal is not None
        and rsi_value is not None
        and macd_now > macd_signal
        and rsi_value > float(params.get("macd_rsi_midline", 50))
    )

    obv_period = int(params.get("obv_period", 20))
    price_period = int(params.get("price_period", 10))
    obv = _obv_series(closes, volumes)
    obv_sma_now = _sma(obv, obv_period)
    obv_sma_prev = _sma(obv[:-1], obv_period)
    price_sma = _sma(closes, price_period)
    obv_price_ok = (
        obv_sma_now is not None
        and obv_sma_prev is not None
        and price_sma is not None
        and obv_sma_now > obv_sma_prev
        and closes[-1] > price_sma
    )

    rsi_floor = float(params.get("rsi_floor", 25))
    rsi_not_extreme = rsi_value is not None and rsi_value >= rsi_floor

    votes = {
        "supertrend": supertrend_ok,
        "ema_alignment": ema_alignment_ok,
        "macd_rsi": macd_rsi_ok,
        "obv_price": obv_price_ok,
        "rsi_not_extreme": rsi_not_extreme,
    }
    values = {
        "atr": atr_value,
        "fast_ema": fast_ema,
        "slow_ema": slow_ema,
        "macd": macd_now,
        "macd_signal": macd_signal,
        "rsi": rsi_value,
        "obv_sma": obv_sma_now,
        "price_sma": price_sma,
    }
    return votes, values


def default_params() -> dict[str, Any]:
    return {
        "profit_target": 0.0525,
        "max_open_entries": 6,
        "min_aligned_conditions": 3,
        "swing_lookback": 24,
        "max_swing_low_distance": 0.03,
        "quote_fraction": 0.12,
        "base_fraction": 1.0,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 150,
        "initial_budget_quote": None,
        "supertrend_period": 10,
        "supertrend_multiplier": 3.0,
        "ema_periods": [8, 13, 21, 34, 55],
        "dual_ema_fast": 9,
        "dual_ema_slow": 21,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "macd_rsi_midline": 50,
        "rsi_period": 14,
        "rsi_floor": 25,
        "obv_period": 20,
        "price_period": 10,
    }


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    bot_id = str(params.get("bot_id") or "").strip()
    symbol = str(params.get("symbol") or "").strip()
    execution_mode = str(params.get("execution_mode") or "testnet").strip()

    closes = [float(x[4]) for x in ohlcv]
    highs = [float(x[2]) for x in ohlcv]
    lows = [float(x[3]) for x in ohlcv]
    volumes = [float(x[5]) for x in ohlcv]
    n = len(closes)
    min_bars = max(
        int(params.get("ohlcv_limit", 150)) // 2,
        int(params.get("macd_slow", 26))
        + int(params.get("macd_signal", 9))
        + 2,
        int(params.get("supertrend_period", 10)) + 2,
    )
    if n < min_bars:
        return SignalResult("hold", {}, n, warmup=True)

    close = closes[-1]
    if close <= 0:
        return SignalResult("hold", {"error": "invalid_close"}, n)

    if not bot_id or not symbol:
        return SignalResult("hold", {"error": "missing_bot_context"}, n)

    profit_target = max(0.0, float(params.get("profit_target", 0.0525)))
    entries = _load_open_entries(bot_id, execution_mode, symbol)

    for entry in entries:
        entry_price = _f(entry.get("entry_price"))
        quantity = _f(entry.get("quantity"))
        target_price = entry_price * (1.0 + profit_target)
        if (
            entry_price > 0
            and quantity > _EPS
            and close >= target_price
            and close >= entry_price
        ):
            return SignalResult(
                "sell",
                {
                    "close": round(close, 6),
                    "profit_target": profit_target,
                    "target_entry_id": int(entry["entry_id"]),
                    "target_entry_price": round(entry_price, 8),
                    "target_price": round(target_price, 8),
                    "sell_quantity": quantity,
                    "open_entries": len(entries),
                    "unrealized_entry_profit_pct": round(
                        (close / entry_price - 1.0) * 100.0,
                        4,
                    ),
                },
                n,
                confidence=1.0,
            )

    votes, values = _directional_votes(highs, lows, closes, volumes, params)
    aligned = sum(1 for value in votes.values() if value is True)
    swing_lookback = max(2, int(params.get("swing_lookback", 24)))
    recent_lows = lows[-min(swing_lookback, len(lows)):]
    swing_low = min(recent_lows)
    distance_from_low = (
        (close - swing_low) / swing_low if swing_low > 0 else 1.0
    )
    near_swing_low = distance_from_low <= float(
        params.get("max_swing_low_distance", 0.03)
    )

    max_open_entries = max(1, int(params.get("max_open_entries", 6)))
    at_max_entries = len(entries) >= max_open_entries
    initial_budget = _f(params.get("initial_budget_quote"))
    budget_exhausted = (
        initial_budget > 0
        and _total_open_cost(entries) >= initial_budget
    )

    can_buy = (
        votes["supertrend"] is True
        and aligned >= int(params.get("min_aligned_conditions", 3))
        and near_swing_low
        and not at_max_entries
        and not budget_exhausted
    )

    meta = {
        "close": round(close, 6),
        "profit_target": profit_target,
        "open_entries": len(entries),
        "max_open_entries": max_open_entries,
        "aligned_conditions": aligned,
        "near_swing_low": near_swing_low,
        "swing_low": round(swing_low, 6),
        "distance_from_swing_low_pct": round(distance_from_low * 100.0, 4),
        "budget_open_cost": round(_total_open_cost(entries), 6),
        "budget_exhausted": budget_exhausted,
        "votes": votes,
        # Reuse the existing voter card/feed plumbing for this strategy's
        # composite buy filters. False/unknown filters are neutral holds.
        "voter_signals": {
            key: _vote_to_signal(value)
            for key, value in votes.items()
        },
        "voter_confidences": {
            key: 1.0 if value is True else 0.0
            for key, value in votes.items()
        },
        "consensus_score": round(aligned / max(1, len(votes)), 4),
        "indicator_values": {
            key: round(value, 6) if value is not None else None
            for key, value in values.items()
        },
    }

    confidence = round(min(1.0, aligned / max(1, len(votes))), 4)
    return SignalResult(
        "buy" if can_buy else "hold",
        meta,
        n,
        confidence=confidence,
    )


def on_buy_filled(
    *,
    bot_id: str,
    symbol: str,
    execution_mode: str,
    order: dict[str, Any],
    fallback_entry_price: float,
    fallback_quantity: float,
) -> None:
    quantity = (
        _f(order.get("filled"))
        or _f(order.get("amount"))
        or fallback_quantity
    )
    cost = _f(order.get("cost"))
    entry_price = _f(order.get("average")) or fallback_entry_price
    if entry_price <= 0 and cost > 0 and quantity > 0:
        entry_price = cost / quantity
    if quantity <= _EPS or entry_price <= 0:
        return

    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO strategy_open_entries (
                bot_id, execution_mode, symbol, entry_price, quantity,
                exchange_order_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_id,
                execution_mode,
                symbol,
                entry_price,
                quantity,
                str(order.get("id")) if order.get("id") is not None else None,
                int(time.time() * 1000),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def on_sell_filled(
    *,
    bot_id: str,
    symbol: str,
    execution_mode: str,
    order: dict[str, Any],
    target_entry_id: int | None,
    fallback_quantity: float,
) -> None:
    if target_entry_id is None:
        return
    sold_quantity = (
        _f(order.get("filled"))
        or _f(order.get("amount"))
        or fallback_quantity
    )
    if sold_quantity <= _EPS:
        return

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT quantity
            FROM strategy_open_entries
            WHERE entry_id = ?
              AND bot_id = ?
              AND execution_mode = ?
              AND symbol = ?
            """,
            (target_entry_id, bot_id, execution_mode, symbol),
        )
        row = cur.fetchone()
        if row is None:
            return

        remaining = max(0.0, float(row["quantity"] or 0.0) - sold_quantity)
        if remaining <= _EPS:
            cur.execute(
                """
                DELETE FROM strategy_open_entries
                WHERE entry_id = ?
                  AND bot_id = ?
                  AND execution_mode = ?
                  AND symbol = ?
                """,
                (target_entry_id, bot_id, execution_mode, symbol),
            )
        else:
            cur.execute(
                """
                UPDATE strategy_open_entries
                SET quantity = ?
                WHERE entry_id = ?
                  AND bot_id = ?
                  AND execution_mode = ?
                  AND symbol = ?
                """,
                (remaining, target_entry_id, bot_id, execution_mode, symbol),
            )
        conn.commit()
    finally:
        conn.close()
