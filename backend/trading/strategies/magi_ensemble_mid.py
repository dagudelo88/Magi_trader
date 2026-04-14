"""
Magi Ensemble MID-FREQUENCY strategy — 5-minute candles (recommended).

Best balance of signal quality vs. trade frequency. Voter set mixes momentum
and mean-reversion voters with meaningful non-hold rates on 5m candles:
  obv_price (15%), stochastic (8.7%), macd_rsi, tema (6%), donchian (1.5%)

Consensus threshold 0.20 — net 1 extra voter on winning side required.

Default params are defined in trading/strategy_templates.py.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.ensemble_core import run_consensus, build_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_ensemble_mid")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    return build_signal_result(run_consensus(ohlcv, params), params)
