"""
Magi Lag Ensemble HIGH-FREQUENCY strategy — 1-minute candles.

BTC → Altcoin lead/lag arbitrage using per-second microstructure data from
market_ticks. directional_net 0.20: with 4 voters, a single buy voter
(net = 0.25) is enough to trade. Best on ETH/USDT, BNB/USDT.

target_asset defaults to the bot symbol at runtime.

Default params are defined in trading/strategy_templates.py.
"""
from __future__ import annotations

from typing import Any

from trading.strategy_templates import get_ensemble_defaults
from trading.strategies.base import SignalResult
from trading.strategies.lag_helpers import get_latest_lag_features
from trading.strategies.lag_ensemble_core import run_lag_consensus, build_lag_signal_result


def default_params() -> dict[str, Any]:
    return get_ensemble_defaults("magi_lag_ensemble_high")


def evaluate(ohlcv: list[list], params: dict[str, Any]) -> SignalResult:
    target_asset: str = params.get("target_asset") or params.get("symbol", "ETH/USDT")
    lag_features = get_latest_lag_features(target_asset, int(params.get("lag_lookback_sec", 60)))
    consensus = run_lag_consensus(ohlcv, params, lag_features)
    return build_lag_signal_result(consensus, params, target_asset)
