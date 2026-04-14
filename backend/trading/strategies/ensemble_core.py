"""
Shared consensus voting engine for all Magi Ensemble strategies.

This module is pure Python with no I/O — no REST calls, no database reads,
no WebSocket access. All OHLCV data is pre-fetched by bot_runner and passed
in. Adding more voters costs zero extra Binance API calls.

Usage:
    from trading.strategies.ensemble_core import run_consensus, build_signal_result
    result = run_consensus(ohlcv, params)
    return build_signal_result(result, params)
"""
from __future__ import annotations

import logging
from typing import Any

from trading.strategies.base import SignalResult

logger = logging.getLogger(__name__)

# Valid voter names — must match entries in registry._STRATEGY_MODULE_MAP.
# Ensemble strategies that reference themselves are blocked (no recursion).
_ENSEMBLE_NAMES = {
    "magi_ensemble_high",
    "magi_ensemble_mid",
    "magi_ensemble_low",
    # Lag variants — blocked to prevent nesting inside classic ensembles.
    "magi_lag_ensemble_high",
    "magi_lag_ensemble_mid",
    "magi_lag_ensemble_low",
}

VALID_VOTER_NAMES: frozenset[str] = frozenset(
    {
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


def run_consensus(ohlcv: list[list], params: dict[str, Any]) -> dict[str, Any]:
    """
    Run all configured voters against the same OHLCV data and apply the
    configured consensus rule to produce a single signal.

    Returns a dict with:
        signal          – "buy" | "sell" | "hold"
        consensus_score – fraction of total weight behind the winning signal
        voter_signals   – {voter_name: signal} for every voter
        meta_weights    – {voter_name: weight} actually used this cycle
        close_count     – number of closes available (max across voters)
        warmup          – True if any voter was in warmup mode
    """
    # Import here to avoid circular imports at module load time.
    from trading.strategies.registry import get_strategy
    from trading.metatrader import get_metatrader

    voters: list[str] = params.get("voters", [])
    consensus_mode: str = params.get("consensus_mode", "majority")
    consensus_threshold: float = float(
        params.get("consensus_threshold", 0.55)
    )

    # Start from configured base weights; MetaTrader can shift them
    # dynamically once it has accumulated enough labeled feedback.
    base_weights: dict[str, float] = dict(
        params.get("voter_weights", {v: 1.0 for v in voters})
    )
    dynamic_weights = get_metatrader().get_dynamic_weights(voters)
    # Dynamic weights override base weights only for keys MetaTrader learned.
    weights: dict[str, float] = {**base_weights, **dynamic_weights}

    votes: dict[str, float] = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
    voter_signals: dict[str, str] = {}
    voter_confidences: dict[str, float | None] = {}
    close_count: int = 0
    any_warmup: bool = False

    for voter_name in voters:
        if voter_name in _ENSEMBLE_NAMES:
            logger.warning(
                "Voter %r is an ensemble — skipped (recursion guard).",
                voter_name,
            )
            voter_signals[voter_name] = "hold"
            voter_confidences[voter_name] = None
            votes["hold"] += weights.get(voter_name, 1.0)
            continue

        if voter_name not in VALID_VOTER_NAMES:
            logger.warning("Unknown voter %r — skipped.", voter_name)
            voter_signals[voter_name] = "hold"
            voter_confidences[voter_name] = None
            votes["hold"] += 1.0
            continue

        try:
            mod = get_strategy(voter_name)
            voter_params = {
                **mod.default_params(),
                **params.get(f"{voter_name}_params", {}),
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
                "Voter %r raised an exception — defaulting to hold.",
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
    Apply the configured consensus rule.

    Returns (signal, reported_score) where reported_score is the value
    most meaningful for the chosen mode.

    Modes:
        majority        – winning signal must hold >50% of total weight
        threshold       – winning signal must hold >= consensus_threshold
        weighted        – alias for threshold
        unanimous       – every voter must agree; otherwise hold
        directional_net – net = (buy_w - sell_w) / total_w; trade when
                          abs(net) > threshold. Ignores passive hold voters,
                          so a single strongly-active minority can trigger.
    """
    if mode == "unanimous":
        unique = set(voter_signals.values())
        if len(unique) == 1 and unique != {"hold"}:
            return next(iter(unique)), score
        return "hold", score

    if mode == "directional_net":
        net = (votes["buy"] - votes["sell"]) / total_weight if total_weight > 0 else 0.0
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

    # Fallback: same as majority
    return (winning_signal if score > 0.5 else "hold"), score


def build_signal_result(
    consensus: dict[str, Any], params: dict[str, Any]
) -> "SignalResult":
    """
    Convert a run_consensus() result dict into a SignalResult.

    Shared by all classic ensemble strategies so their evaluate() functions
    stay as one-liners.
    """
    meta: dict[str, Any] = {
        "consensus_score": consensus["consensus_score"],
        "voter_signals": consensus["voter_signals"],
        "meta_weights": consensus["meta_weights"],
        "voter_count": len(consensus["voter_signals"]),
        "consensus_mode": params.get("consensus_mode", "directional_net"),
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
