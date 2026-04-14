"""
Central strategy registry.

Every strategy module must export:
    evaluate(ohlcv: list[list], params: dict) -> SignalResult
    default_params() -> dict[str, Any]

Bot runner and API look up strategies by name through this module.
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from trading.strategies.base import SignalResult

# Lazy-import map: name → module path suffix (relative to trading.strategies)
_STRATEGY_MODULE_MAP: dict[str, str] = {
    "sma_cross": "trading.strategies.sma_cross",
    "supertrend": "trading.strategies.supertrend",
    "bb_rsi": "trading.strategies.bb_rsi",
    "macd_rsi": "trading.strategies.macd_rsi",
    "rsi_cross": "trading.strategies.rsi_cross",
    "ema_ribbon": "trading.strategies.ema_ribbon",
    "dual_ema": "trading.strategies.dual_ema",
    "stochastic": "trading.strategies.stochastic",
    "bb_breakout": "trading.strategies.bb_breakout",
    "parabolic_sar": "trading.strategies.parabolic_sar",
    "donchian": "trading.strategies.donchian",
    "tema": "trading.strategies.tema",
    "cci": "trading.strategies.cci",
    "obv_price": "trading.strategies.obv_price",
    "price_breakout": "trading.strategies.price_breakout",
    "spot_grid": "trading.strategies.spot_grid",
    # Magi Ensemble — many voters → one consensus signal → one bot execution
    "magi_ensemble_high": "trading.strategies.magi_ensemble_high",
    "magi_ensemble_mid": "trading.strategies.magi_ensemble_mid",
    "magi_ensemble_low": "trading.strategies.magi_ensemble_low",
    # Magi Lag Ensemble — BTC-alt lead/lag specialization (uses market_ticks microstructure)
    "magi_lag_ensemble_high": "trading.strategies.magi_lag_ensemble_high",
    "magi_lag_ensemble_mid": "trading.strategies.magi_lag_ensemble_mid",
    "magi_lag_ensemble_low": "trading.strategies.magi_lag_ensemble_low",
    # Lag voters (used inside Magi Lag Ensembles — not intended as standalone bots)
    "btc_lead_detector": "trading.strategies.btc_lead_detector",
    "roc_divergence": "trading.strategies.roc_divergence",
    "lag_correlation": "trading.strategies.lag_correlation",
    "ratio_mean_reversion": "trading.strategies.ratio_mean_reversion",
}

# Human-readable display names for the UI
STRATEGY_DISPLAY_NAMES: dict[str, str] = {
    "sma_cross": "SMA Cross",
    "supertrend": "Supertrend",
    "bb_rsi": "Bollinger Bands + RSI",
    "macd_rsi": "MACD + RSI",
    "rsi_cross": "RSI Crossover",
    "ema_ribbon": "EMA Ribbon",
    "dual_ema": "Dual EMA",
    "stochastic": "Stochastic %K/%D",
    "bb_breakout": "BB Breakout",
    "parabolic_sar": "Parabolic SAR",
    "donchian": "Donchian Channel",
    "tema": "Triple EMA (TEMA)",
    "cci": "CCI",
    "obv_price": "OBV + Price",
    "price_breakout": "Price Breakout",
    "spot_grid": "Spot Grid",
    "magi_ensemble_high": "Magi Ensemble — High Frequency",
    "magi_ensemble_mid": "Magi Ensemble — Mid Frequency",
    "magi_ensemble_low": "Magi Ensemble — Low Frequency",
    "magi_lag_ensemble_high": "Magi Lag Ensemble — High Frequency",
    "magi_lag_ensemble_mid": "Magi Lag Ensemble — Mid Frequency",
    "magi_lag_ensemble_low": "Magi Lag Ensemble — Low Frequency",
    "btc_lead_detector": "BTC Lead Detector",
    "roc_divergence": "ROC Divergence",
    "lag_correlation": "Lag Correlation",
    "ratio_mean_reversion": "Ratio Mean Reversion",
}

_cache: dict[str, Any] = {}


def _import(name: str) -> Any:
    import importlib
    module_path = _STRATEGY_MODULE_MAP[name]
    mod = importlib.import_module(module_path)
    return mod


def get_strategy(name: str) -> Any:
    """Return the strategy module for `name`. Raises ValueError if unknown."""
    if name not in _STRATEGY_MODULE_MAP:
        available = list(_STRATEGY_MODULE_MAP.keys())
        raise ValueError(f"Unknown strategy {name!r}. Available: {available}")
    if name not in _cache:
        _cache[name] = _import(name)
    return _cache[name]


def strategy_names() -> list[str]:
    """Ordered list of all registered strategy names."""
    return list(_STRATEGY_MODULE_MAP.keys())


def strategy_catalog() -> list[dict[str, Any]]:
    """
    Returns a list of dicts suitable for the frontend strategy picker.
    Each entry includes name, display_name, and default_params.
    """
    result = []
    for name in _STRATEGY_MODULE_MAP:
        mod = get_strategy(name)
        result.append(
            {
                "name": name,
                "display_name": STRATEGY_DISPLAY_NAMES.get(name, name),
                "default_params": mod.default_params(),
            }
        )
    return result


def default_params_for(strategy_name: str) -> dict[str, Any]:
    """Return default params dict for the given strategy."""
    return get_strategy(strategy_name).default_params()
