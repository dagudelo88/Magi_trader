"""
Magi Lag Ensemble LOW-FREQUENCY strategy — 15-minute candles.

High-conviction lag trades with 3 voters (dropping lag_correlation which is
noisier at short windows). directional_net 0.35 ≈ 2/3 majority — equivalent
to the old 65% threshold but resistant to all-hold bias.

Default params are defined in trading/strategy_templates.py.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.lag_helpers import get_latest_lag_features
from trading.strategies.lag_ensemble_core import run_lag_consensus, build_lag_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_lag_ensemble_low")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    target_asset: str = params.get("target_asset") or params.get("symbol", "ETH/USDT")
    lag_features = get_latest_lag_features(target_asset, int(params.get("lag_lookback_sec", 60)))
    consensus = run_lag_consensus(ohlcv, params, lag_features)
    return build_lag_signal_result(consensus, params, target_asset)
