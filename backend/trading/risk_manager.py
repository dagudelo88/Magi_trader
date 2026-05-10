from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import pstdev
from typing import Any

from trading.bot_performance import (
    compute_closed_trades,
    compute_strategy_performance,
)
from trading.risk_settings import normalize_risk_settings


@dataclass
class RiskDecision:
    allowed: bool
    reason: str | None = None
    should_pause: bool = False
    size_multiplier: float = 1.0
    risk_pct: float | None = None
    current_capital: float | None = None
    daily_pnl: float | None = None
    drawdown_pct: float | None = None
    consecutive_losses: int = 0
    volatility_pct: float | None = None


def dynamic_risk_pct(
    settings: dict[str, Any],
    consensus_score: float | None,
) -> float:
    cfg = normalize_risk_settings(settings)
    base = float(cfg["base_risk_pct"])
    if not cfg["enable_dynamic_sizing"]:
        return base
    score = consensus_score if consensus_score is not None else 0.50
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 0.50
    score = max(0.0, min(1.0, score))
    for tier in cfg["dynamic_tiers"]:
        min_score = tier.get("min_score")
        max_score = tier.get("max_score")
        if min_score is not None and score < float(min_score):
            continue
        if max_score is not None and score >= float(max_score):
            continue
        return base * float(tier["multiplier"])
    return base


def recent_volatility_pct(
    ohlcv: list[Any],
    lookback: int = 20,
) -> float | None:
    closes: list[float] = []
    for candle in ohlcv[-lookback:]:
        try:
            closes.append(float(candle[4]))
        except (TypeError, ValueError, IndexError):
            continue
    if len(closes) < 3:
        return None
    returns: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev > 0:
            returns.append((cur - prev) / prev)
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(len(returns)) * 100.0


def _start_of_utc_day_ms(now_ms: int | None = None) -> int:
    fallback_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    now = datetime.fromtimestamp((now_ms or fallback_ms) / 1000, tz=UTC)
    start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    return int(start.timestamp() * 1000)


def _daily_pnl(
    trades: list[dict[str, Any]],
    now_ms: int | None = None,
) -> float:
    start_ms = _start_of_utc_day_ms(now_ms)
    total = 0.0
    for trade in trades:
        ts = trade.get("timestamp")
        if ts is None:
            continue
        try:
            if int(ts) >= start_ms:
                total += float(trade.get("realized_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _consecutive_losses(trades: list[dict[str, Any]]) -> int:
    losses = 0
    for trade in reversed(trades):
        outcome = str(trade.get("outcome") or "")
        if outcome == "loss":
            losses += 1
            continue
        if outcome == "flat":
            continue
        break
    return losses


def _drawdown_pct(
    trades: list[dict[str, Any]],
    initial_capital: float,
    *,
    current_capital: float | None,
) -> float:
    capital = initial_capital
    peak = initial_capital
    max_dd = 0.0
    for trade in trades:
        capital += float(trade.get("realized_pnl") or 0.0)
        peak = max(peak, capital)
        if peak > 0:
            max_dd = max(max_dd, ((peak - capital) / peak) * 100.0)
    if current_capital is not None:
        peak = max(peak, current_capital)
        if peak > 0:
            max_dd = max(max_dd, ((peak - current_capital) / peak) * 100.0)
    return max_dd


def evaluate_trade_risk(
    *,
    settings: dict[str, Any],
    orders_oldest_first: list[dict[str, Any]],
    symbol: str,
    initial_capital: float,
    mark_price: float | None,
    consensus_score: float | None,
    ohlcv: list[Any],
    side: str,
    now_ms: int | None = None,
) -> RiskDecision:
    cfg = normalize_risk_settings(settings)
    perf = compute_strategy_performance(
        orders_oldest_first,
        symbol,
        mark_price=mark_price,
    )
    unrealized = float(perf["unrealized_pnl_quote"] or 0.0)
    current_capital = (
        initial_capital + float(perf["realized_pnl_quote"] or 0.0) + unrealized
    )
    trades = compute_closed_trades(orders_oldest_first, symbol)
    daily_pnl = _daily_pnl(trades, now_ms)
    streak = _consecutive_losses(trades)
    drawdown = _drawdown_pct(
        trades,
        initial_capital,
        current_capital=current_capital,
    )
    risk_pct = dynamic_risk_pct(cfg, consensus_score)

    if cfg["enable_daily_loss_limit"] and daily_pnl < 0:
        daily_loss_pct = (
            abs(daily_pnl) / initial_capital * 100.0
            if initial_capital > 0
            else 0.0
        )
        if daily_loss_pct >= float(cfg["daily_loss_limit_pct"]):
            return RiskDecision(
                allowed=False,
                reason=(
                    f"daily loss limit triggered ({daily_loss_pct:.2f}% >= "
                    f"{float(cfg['daily_loss_limit_pct']):.2f}%)"
                ),
                should_pause=True,
                risk_pct=risk_pct,
                current_capital=current_capital,
                daily_pnl=daily_pnl,
                drawdown_pct=drawdown,
                consecutive_losses=streak,
            )

    if (
        cfg["enable_consecutive_loss"]
        and streak >= int(cfg["consecutive_loss_limit"])
    ):
        return RiskDecision(
            allowed=False,
            reason=f"consecutive loss breaker triggered ({streak} losses)",
            should_pause=True,
            risk_pct=risk_pct,
            current_capital=current_capital,
            daily_pnl=daily_pnl,
            drawdown_pct=drawdown,
            consecutive_losses=streak,
        )

    size_multiplier = 1.0
    if (
        cfg["enable_drawdown_protection"]
        and drawdown >= float(cfg["max_drawdown_pct"])
    ):
        if cfg["drawdown_action"] == "pause":
            return RiskDecision(
                allowed=False,
                reason=(
                    f"drawdown protection triggered ({drawdown:.2f}% >= "
                    f"{float(cfg['max_drawdown_pct']):.2f}%)"
                ),
                should_pause=True,
                risk_pct=risk_pct,
                current_capital=current_capital,
                daily_pnl=daily_pnl,
                drawdown_pct=drawdown,
                consecutive_losses=streak,
            )
        if side.lower() == "buy":
            size_multiplier = float(cfg["drawdown_reduce_factor"])

    volatility = recent_volatility_pct(ohlcv)
    if (
        cfg["enable_volatility_pause"]
        and volatility is not None
        and cfg["volatility_threshold"] is not None
        and volatility >= float(cfg["volatility_threshold"])
    ):
        return RiskDecision(
            allowed=False,
            reason=(
                f"volatility pause triggered ({volatility:.2f}% >= "
                f"{float(cfg['volatility_threshold']):.2f}%)"
            ),
            should_pause=False,
            risk_pct=risk_pct,
            current_capital=current_capital,
            daily_pnl=daily_pnl,
            drawdown_pct=drawdown,
            consecutive_losses=streak,
            volatility_pct=volatility,
        )

    return RiskDecision(
        allowed=True,
        size_multiplier=size_multiplier,
        risk_pct=risk_pct,
        current_capital=current_capital,
        daily_pnl=daily_pnl,
        drawdown_pct=drawdown,
        consecutive_losses=streak,
        volatility_pct=volatility,
    )

