"""
Single source of truth for all Magi ensemble strategy parameters.

Every ensemble strategy's default_params() delegates here. The
/api/strategies endpoint serves these to the frontend via strategy_catalog(),
so the frontend never needs to hardcode strategy params.

To tune a strategy globally: edit the values in ENSEMBLE_TEMPLATES.
To tune a running bot individually: PATCH /api/bots/{id}/strategy-params.
"""
from __future__ import annotations

from typing import Any

# fmt: off
ENSEMBLE_TEMPLATES: dict[str, dict[str, Any]] = {

    # ── Classic Magi Ensembles (OHLCV candles) ──────────────────────────────

    "magi_ensemble_high": {
        # Execution
        "quote_fraction":          0.03,
        "base_fraction":           0.6,
        "min_trade_interval_sec":  60,
        "ohlcv_timeframe":         "1m",
        "ohlcv_limit":             300,
        "initial_budget_quote":    None,
        # Voters — top active classic voters (non-hold rates: ema_ribbon 57%,
        # obv_price 47%, stochastic 27%, macd_rsi 14%, cci 14%)
        "voters": ["ema_ribbon", "obv_price", "stochastic", "macd_rsi", "cci"],
        "consensus_mode":          "directional_net",
        # net = (buy_w - sell_w) / total_w; fires when abs(net) > 0.15
        "consensus_threshold":     0.15,
        "voter_weights": {
            "ema_ribbon":  1.0,
            "obv_price":   1.2,
            "stochastic":  1.1,
            "macd_rsi":    1.0,
            "cci":         1.1,
        },
    },

    "magi_ensemble_mid": {
        "quote_fraction":          0.04,
        "base_fraction":           0.6,
        "min_trade_interval_sec":  300,
        "ohlcv_timeframe":         "5m",
        "ohlcv_limit":             200,
        "initial_budget_quote":    None,
        # Voters — mid-activity voters suitable for 5m candles (non-hold rates:
        # obv_price 15%, stochastic 8.7%, macd_rsi stable, tema 6%, donchian 1.5%)
        "voters": ["obv_price", "stochastic", "macd_rsi", "tema", "donchian"],
        "consensus_mode":          "directional_net",
        "consensus_threshold":     0.20,
        "voter_weights": {
            "obv_price":  1.2,
            "stochastic": 1.1,
            "macd_rsi":   1.2,
            "tema":       1.0,
            "donchian":   1.0,
        },
    },

    "magi_ensemble_low": {
        "quote_fraction":          0.05,
        "base_fraction":           0.7,
        "min_trade_interval_sec":  1800,
        "ohlcv_timeframe":         "1h",
        "ohlcv_limit":             150,
        "initial_budget_quote":    None,
        # Voters — trend + breakout mix for high-conviction 1h entries
        "voters": ["supertrend", "ema_ribbon", "donchian", "tema", "price_breakout"],
        "consensus_mode":          "directional_net",
        # 0.25 ≈ net 1.25/5 vote advantage — requires clear direction
        "consensus_threshold":     0.25,
        "voter_weights": {
            "supertrend":    1.3,
            "ema_ribbon":    1.2,
            "donchian":      1.0,
            "tema":          1.0,
            "price_breakout": 1.1,
        },
    },

    # ── Magi Lag Ensembles (microstructure market_ticks + OHLCV) ────────────

    "magi_lag_ensemble_high": {
        "quote_fraction":          0.03,
        "base_fraction":           0.6,
        "min_trade_interval_sec":  60,
        "ohlcv_timeframe":         "1m",
        "ohlcv_limit":             200,
        "initial_budget_quote":    None,
        # Lag-specific params (target_asset defaults to bot symbol at runtime)
        "target_asset":            None,
        "lag_lookback_sec":        60,
        "voters": [
            "btc_lead_detector", "roc_divergence",
            "lag_correlation", "ratio_mean_reversion",
        ],
        "consensus_mode":          "directional_net",
        # With 4 voters, net = 1/4 = 0.25 — even a single buy voter fires
        "consensus_threshold":     0.20,
        "voter_weights": {
            "btc_lead_detector":   1.3,
            "roc_divergence":      1.2,
            "lag_correlation":     1.1,
            "ratio_mean_reversion": 1.0,
        },
    },

    "magi_lag_ensemble_mid": {
        "quote_fraction":          0.025,
        "base_fraction":           0.6,
        "min_trade_interval_sec":  300,
        "ohlcv_timeframe":         "5m",
        "ohlcv_limit":             200,
        "initial_budget_quote":    None,
        "target_asset":            None,
        "lag_lookback_sec":        60,
        "voters": [
            "btc_lead_detector", "roc_divergence",
            "lag_correlation", "ratio_mean_reversion",
        ],
        "consensus_mode":          "directional_net",
        "consensus_threshold":     0.25,
        "voter_weights": {
            "btc_lead_detector":   1.3,
            "roc_divergence":      1.2,
            "lag_correlation":     1.1,
            "ratio_mean_reversion": 1.0,
        },
    },

    "magi_lag_ensemble_low": {
        "quote_fraction":          0.04,
        "base_fraction":           0.65,
        "min_trade_interval_sec":  900,
        "ohlcv_timeframe":         "15m",
        "ohlcv_limit":             150,
        "initial_budget_quote":    None,
        "target_asset":            None,
        "lag_lookback_sec":        60,
        # 3 voters — drop lag_correlation (noisier at short windows)
        "voters": ["btc_lead_detector", "roc_divergence", "ratio_mean_reversion"],
        "consensus_mode":          "directional_net",
        # 0.35 with 3 voters ≈ 2/3 majority — high-conviction only
        "consensus_threshold":     0.35,
        "voter_weights": {
            "btc_lead_detector":   1.3,
            "roc_divergence":      1.2,
            "ratio_mean_reversion": 1.1,
        },
    },
}
# fmt: on


def get_ensemble_defaults(strategy_name: str) -> dict[str, Any]:
    """Return a fresh copy of the default params for the given ensemble strategy."""
    if strategy_name not in ENSEMBLE_TEMPLATES:
        raise KeyError(
            f"No ensemble template defined for {strategy_name!r}. "
            f"Available: {list(ENSEMBLE_TEMPLATES)}"
        )
    return dict(ENSEMBLE_TEMPLATES[strategy_name])
