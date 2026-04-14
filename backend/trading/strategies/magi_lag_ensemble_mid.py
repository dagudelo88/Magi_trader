"""
Magi Lag Ensemble MID-FREQUENCY strategy — 5-minute candles (recommended).

Best balance of signal quality and trade frequency for BTC-alt lag strategies.
directional_net 0.25 reduces microstructure noise vs. the high-frequency
variant. Best on ETH/USDT, SOL/USDT, BNB/USDT.

Default params are defined in trading/strategy_templates.py.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.lag_helpers import get_latest_lag_features
from trading.strategies.lag_ensemble_core import run_lag_consensus, build_lag_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_lag_ensemble_mid")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    target_asset: str = params.get("target_asset") or params.get("symbol", "ETH/USDT")
    lag_features = get_latest_lag_features(target_asset, int(params.get("lag_lookback_sec", 60)))
    consensus = run_lag_consensus(ohlcv, params, lag_features)
    return build_lag_signal_result(consensus, params, target_asset)
