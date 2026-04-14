"""
Magi Ensemble HIGH-FREQUENCY strategy — 1-minute scalping.

Voter committee chairman: polls all configured voters on the same OHLCV data,
applies directional_net consensus, and returns a single SignalResult.

Role distinction
----------------
Voters  – pure functions; zero Binance API calls.
Bot     – the single execution unit that places one order per non-hold signal.

Default params are defined in trading/strategy_templates.py.
Tune globally there; tune a running bot via PATCH /api/bots/{id}/strategy-params.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.ensemble_core import run_consensus, build_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_ensemble_high")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    return build_signal_result(run_consensus(ohlcv, params), params)
