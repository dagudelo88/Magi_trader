"""
Lag-aware consensus voting engine for Magi Lag Ensemble strategies.

Mirrors the API of ``ensemble_core.run_consensus`` but additionally:

1. Accepts pre-fetched ``lag_features`` from ``lag_helpers`` and injects them
   into each voter's params so lag voters can read microstructure data without
   touching the DB themselves.
2. Validates voters against ``VALID_LAG_VOTER_NAMES`` (a dedicated allowlist
   separate from the OHLCV-only voters in ensemble_core).

This module is intentionally I/O-free: ``lag_features`` are passed *in* by
the caller (the lag ensemble's ``evaluate()``), keeping it pure and testable.
"""
from __future__ import annotations

import logging
from typing import Any

from trading.strategies.base import SignalResult

logger = logging.getLogger(__name__)

# Lag ensemble variants — blocked as voters to prevent recursive nesting.
_LAG_ENSEMBLE_NAMES: frozenset[str] = frozenset(
    {
        "magi_lag_ensemble_high",
        "magi_lag_ensemble_mid",
        "magi_lag_ensemble_low",
        # Also block classic ensembles from nesting inside a lag ensemble.
        "magi_ensemble_high",
        "magi_ensemble_mid",
        "magi_ensemble_low",
    }
)

# Strategies that can serve as voters inside a lag ensemble.
# These are the four lag-specialized voters plus any classic OHLCV voter for
# hybrid setups (classic voters simply ignore the injected lag_features key).
VALID_LAG_VOTER_NAMES: frozenset[str] = frozenset(
    {
        # Lag-specialized voters
        "btc_lead_detector",
        "roc_divergence",
        "lag_correlation",
        "ratio_mean_reversion",
        # Classic OHLCV voters (optional — for hybrid ensembles)
        "sma_cross",
        "supertrend",
        "bb_rsi",
        "macd_rsi",
        "rsi_cross",
        "ema_ribbon",
        "dual_ema",
        "stochastic",
        "bb_breakout",
        "parabolic_sar",
        "donchian",
        "tema",
        "cci",
        "obv_price",
        "price_breakout",
    }
)


def run_lag_consensus(
    ohlcv: list[list],
    params: dict[str, Any],
    lag_features: dict[str, Any],
) -> dict[str, Any]:
    """
    Run all configured lag voters and apply the configured consensus rule.

    ``lag_features`` is injected into each voter's params under the
    ``"lag_features"`` key so voters can access microstructure data without
    any DB calls of their own.

    Args:
        ohlcv:        OHLCV bars pre-fetched by bot_runner.
        params:       Ensemble-level params (voters, weights, thresholds).
        lag_features: Output of ``lag_helpers.get_latest_lag_features()``.

    Returns:
        {
            signal          – "buy" | "sell" | "hold"
            consensus_score – fraction of total weight behind winning signal
            voter_signals   – {voter_name: signal} for every voter
            meta_weights    – {voter_name: weight} actually used this cycle
            close_count     – number of closes available (max across voters)
            warmup          – True if any voter was in warmup mode
            lag_tick_count  – number of market_ticks rows available
        }
    """
    from trading.strategies.registry import get_strategy
    from trading.metatrader import get_metatrader

    voters: list[str] = params.get("voters", [])
    consensus_mode: str = params.get("consensus_mode", "threshold")
    consensus_threshold: float = float(params.get("consensus_threshold", 0.60))

    base_weights: dict[str, float] = dict(
        params.get("voter_weights", {v: 1.0 for v in voters})
    )
    dynamic_weights = get_metatrader().get_dynamic_weights(voters)
    weights: dict[str, float] = {**base_weights, **dynamic_weights}

    votes: dict[str, float] = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
    voter_signals: dict[str, str] = {}
    voter_confidences: dict[str, float | None] = {}
    close_count: int = 0
    any_warmup: bool = False

    for voter_name in voters:
        if voter_name in _LAG_ENSEMBLE_NAMES:
            logger.warning(
                "Voter %r is an ensemble — skipped to prevent recursion.",
                voter_name,
            )
            voter_signals[voter_name] = "hold"
            voter_confidences[voter_name] = None
            votes["hold"] += weights.get(voter_name, 1.0)
            continue

        if voter_name not in VALID_LAG_VOTER_NAMES:
            logger.warning("Unknown lag voter %r — skipped.", voter_name)
            voter_signals[voter_name] = "hold"
            voter_confidences[voter_name] = None
            votes["hold"] += 1.0
            continue

        try:
            mod = get_strategy(voter_name)
            voter_params: dict[str, Any] = {
                **mod.default_params(),
                **params.get(f"{voter_name}_params", {}),
                # Inject lag_features so voters can access microstructure data.
                "lag_features": lag_features,
            }
            voter_result = mod.evaluate(ohlcv, voter_params)
            sig = voter_result.signal
            voter_signals[voter_name] = sig
            voter_confidences[voter_name] = voter_result.confidence
            votes[sig] += weights.get(voter_name, 1.0)
            close_count = max(close_count, voter_result.close_count)
            if voter_result.warmup:
                any_warmup = True
        except Exception:
            logger.exception(
                "Lag voter %r raised an exception — defaulting to hold.",
                voter_name,
            )
            voter_signals[voter_name] = "hold"
            voter_confidences[voter_name] = None
            votes["hold"] += 1.0

    if not voter_signals:
        return {
            "signal": "hold",
            "consensus_score": 0.0,
            "voter_signals": {},
            "voter_confidences": {},
            "meta_weights": weights,
            "close_count": 0,
            "warmup": False,
            "lag_tick_count": lag_features.get("tick_count", 0),
        }

    total_weight = sum(votes.values())
    winning_signal = max(votes, key=lambda s: votes[s])
    score = votes[winning_signal] / total_weight if total_weight > 0 else 0.0

    final_signal, reported_score = _apply_consensus_rule(
        winning_signal,
        score,
        votes,
        total_weight,
        voter_signals,
        consensus_mode,
        consensus_threshold,
    )

    return {
        "signal": final_signal,
        "consensus_score": round(reported_score, 4),
        "voter_signals": voter_signals,
        "voter_confidences": voter_confidences,
        "meta_weights": weights,
        "close_count": close_count,
        "warmup": any_warmup,
        "lag_tick_count": lag_features.get("tick_count", 0),
    }


def _apply_consensus_rule(
    winning_signal: str,
    score: float,
    votes: dict[str, float],
    total_weight: float,
    voter_signals: dict[str, str],
    mode: str,
    threshold: float,
) -> tuple[str, float]:
    """
    Apply the configured consensus rule (mirrors ensemble_core logic).

    Returns (signal, reported_score).
    """
    if mode == "unanimous":
        unique = set(voter_signals.values())
        if len(unique) == 1 and unique != {"hold"}:
            return next(iter(unique)), score
        return "hold", score

    if mode == "directional_net":
        net = (
            (votes["buy"] - votes["sell"]) / total_weight
            if total_weight > 0
            else 0.0
        )
        if net > threshold:
            return "buy", abs(net)
        if net < -threshold:
            return "sell", abs(net)
        return "hold", abs(net)

    if mode in ("majority", "threshold", "weighted"):
        effective = threshold if mode in ("threshold", "weighted") else 0.5
        if score >= effective and winning_signal != "hold":
            return winning_signal, score
        return (winning_signal if score >= effective else "hold"), score

    # Fallback: majority
    return (winning_signal if score > 0.5 else "hold"), score


def build_lag_signal_result(
    consensus: dict[str, Any],
    params: dict[str, Any],
    target_asset: str,
) -> "SignalResult":
    """
    Convert a run_lag_consensus() result into a SignalResult.

    Shared by all lag ensemble strategies so their evaluate() functions
    stay as one-liners.
    """
    meta: dict[str, Any] = {
        "consensus_score": consensus["consensus_score"],
        "voter_signals": consensus["voter_signals"],
        "voter_confidences": consensus.get("voter_confidences", {}),
        "meta_weights": consensus["meta_weights"],
        "voter_count": len(consensus["voter_signals"]),
        "consensus_mode": params.get("consensus_mode", "directional_net"),
        "lag_tick_count": consensus["lag_tick_count"],
        "target_asset": target_asset,
    }
    return SignalResult(
        signal=consensus["signal"],
        meta=meta,
        close_count=consensus["close_count"],
        warmup=consensus["warmup"],
        confidence=(
            consensus["consensus_score"] if consensus["signal"] != "hold" else None
        ),
    )
