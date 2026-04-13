"""
Realized / unrealized PnL and risk metrics from stored spot orders (FIFO lots).

Uses `bot_orders` fields: side, amount, cost, average, filled (CCXT-normalized).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _f(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Lot:
    rem_base: float
    cost_quote: float


def _infer_quote_currency(symbol: str) -> str:
    if "/" in symbol:
        return symbol.split("/")[1].upper()
    return "USDT"


def _buy_base_cost(o: dict[str, Any]) -> tuple[float, float]:
    """Return (base_received, quote_spent) for a buy order."""
    avg = _f(o.get("average"))
    filled = _f(o.get("filled"))
    amt = _f(o.get("amount"))
    cost = _f(o.get("cost"))
    base = filled if filled is not None and filled > 0 else (amt or 0.0)
    if cost is not None and cost > 0:
        quote = cost
    elif avg is not None and base > 0:
        quote = avg * base
    else:
        quote = 0.0
    return (max(0.0, base), max(0.0, quote))


def _sell_base_proceeds(o: dict[str, Any]) -> tuple[float, float]:
    """Return (base_sold, quote_received) for a sell order."""
    avg = _f(o.get("average"))
    filled = _f(o.get("filled"))
    amt = _f(o.get("amount"))
    cost = _f(o.get("cost"))
    base = filled if filled is not None and filled > 0 else (amt or 0.0)
    if cost is not None and cost > 0:
        quote = cost
    elif avg is not None and base > 0:
        quote = avg * base
    else:
        quote = 0.0
    return (max(0.0, base), max(0.0, quote))


def compute_strategy_performance(
    orders_oldest_first: list[dict[str, Any]],
    symbol: str,
    *,
    mark_price: float | None = None,
) -> dict[str, Any]:
    """
    FIFO spot accounting. Each sell order closes inventory and adds one closed-trade outcome
    for win-rate (PnL of that exit vs its cost basis).
    """
    qc = _infer_quote_currency(symbol)
    lots: list[Lot] = []
    realized = 0.0
    closed_trades = 0
    wins = losses = flats = 0
    cumulative_series: list[float] = []
    peak = 0.0
    max_dd_quote = 0.0

    def _after_realized(delta: float) -> None:
        nonlocal realized, peak, max_dd_quote
        realized += delta
        cumulative_series.append(realized)
        if realized > peak:
            peak = realized
        dd = peak - realized
        if dd > max_dd_quote:
            max_dd_quote = dd

    for o in orders_oldest_first:
        side = str(o.get("side") or "").lower()
        if side == "buy":
            b, q = _buy_base_cost(o)
            if b <= 0:
                continue
            lots.append(Lot(rem_base=b, cost_quote=q))
        elif side == "sell":
            need, proceeds = _sell_base_proceeds(o)
            if need <= 0:
                continue
            available = sum(l.rem_base for l in lots)
            matched_base = min(need, available)
            if matched_base <= 1e-12:
                continue
            proceeds = proceeds * (matched_base / need) if need > 1e-12 else 0.0
            basis = 0.0
            rem = matched_base
            while rem > 1e-12 and lots:
                lot = lots[0]
                take = min(rem, lot.rem_base)
                if lot.rem_base > 1e-12:
                    portion_cost = lot.cost_quote * (take / lot.rem_base)
                else:
                    portion_cost = 0.0
                basis += portion_cost
                lot.rem_base -= take
                lot.cost_quote -= portion_cost
                rem -= take
                if lot.rem_base <= 1e-12:
                    lots.pop(0)
            pnl = proceeds - basis
            closed_trades += 1
            if pnl > 1e-8:
                wins += 1
            elif pnl < -1e-8:
                losses += 1
            else:
                flats += 1
            _after_realized(pnl)

    open_base = sum(l.rem_base for l in lots)
    open_basis = sum(l.cost_quote for l in lots)

    unreal: float | None = None
    if mark_price is not None and mark_price > 0 and open_base > 1e-12:
        mtm = mark_price * open_base
        unreal = mtm - open_basis

    wr: float | None = None
    decided = wins + losses + flats
    if decided > 0:
        wr = 100.0 * wins / decided

    max_dd_pct: float | None = None
    if peak > 1e-12:
        max_dd_pct = (max_dd_quote / peak) * 100.0

    return {
        "realized_pnl_quote": realized,
        "unrealized_pnl_quote": unreal,
        "open_base_position": open_base,
        "open_cost_basis_quote": open_basis,
        "closed_trades": closed_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "breakeven_trades": flats,
        "win_rate_pct": wr,
        "max_drawdown_quote": max_dd_quote,
        "max_drawdown_pct": max_dd_pct,
        "quote_currency": qc,
    }
