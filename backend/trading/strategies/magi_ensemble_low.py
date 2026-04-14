"""
Magi Ensemble LOW-FREQUENCY strategy — 1-hour swing trading.

High-conviction entries using trend (supertrend, ema_ribbon) and breakout
(donchian, tema, price_breakout) voters. directional_net 0.25 requires a
clear net directional edge — replaces the old "unanimous" mode which was
impossible to satisfy across voters with very different signal frequencies.

Default params are defined in trading/strategy_templates.py.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.ensemble_core import run_consensus, build_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_ensemble_low")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    return build_signal_result(run_consensus(ohlcv, params), params)
