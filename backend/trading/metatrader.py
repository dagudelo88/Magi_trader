"""
MetaMagi — rule-based voter weight learner (Phase 1: EMA accuracy).

Sits outside any individual bot. After the meta_training_loop labels
`voter_feedback` rows with forward returns, this module updates each voter's
exponential moving accuracy and exposes dynamic weights that the ensemble
core merges with the user-configured base weights every cycle.

Phase 2 upgrade path: replace the EMA logic in `train_step` with a PyTorch
feed-forward net (see docs/magitrade.md §MetaMagi). The public interface
(get_dynamic_weights / train_step) is unchanged so ensemble_core needs no
modification.

All methods are thread-safe via a lightweight lock (bot_runner runs in a
thread pool).  Database access happens only in the training loop (every 30
min), never on the hot path.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

# EMA smoothing factor — higher = faster adaptation, lower = more stable.
_EMA_ALPHA = 0.1
# Minimum weight so that no voter is ever completely silenced.
_MIN_WEIGHT = 0.5
# Maximum weight multiplier relative to the average (caps runaway dominance).
_MAX_WEIGHT_MULTIPLIER = 2.0


class MetaTrader:
    """
    Tracks each voter's exponential moving accuracy and translates it into
    normalised dynamic weights consumed by the ensemble consensus engine.

    Accuracy definition:
        correct = voter_signal matches the forward price direction
        (forward_roc_30s > threshold → buy correct;
         forward_roc_30s < -threshold → sell correct;
         |forward_roc_30s| <= threshold → hold correct)
    """

    def __init__(self, roc_threshold: float = 0.0005) -> None:
        """
        Args:
            roc_threshold: Minimum absolute ROC to count as a directional
                           move. Smaller moves are labelled "hold" outcomes.
        """
        self._roc_threshold = roc_threshold
        self._accuracies: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Hot-path method — must be fast, no I/O
    # ------------------------------------------------------------------

    def get_dynamic_weights(self, voter_names: list[str]) -> dict[str, float]:
        """
        Return normalised dynamic weights for the given voters.

        Voters with no recorded accuracy yet receive weight 1.0 (neutral).
        Weights are scaled so their average equals 1.0 and each is clamped
        to [_MIN_WEIGHT, average * _MAX_WEIGHT_MULTIPLIER].
        """
        with self._lock:
            raw: dict[str, float] = {
                v: self._accuracies.get(v, 0.5) for v in voter_names
            }

        if not raw:
            return {}

        avg_acc = sum(raw.values()) / len(raw)
        if avg_acc <= 0:
            return {v: 1.0 for v in voter_names}

        weights: dict[str, float] = {}
        max_w = avg_acc * _MAX_WEIGHT_MULTIPLIER
        for voter, acc in raw.items():
            w = acc / avg_acc   # scale so average weight == 1.0
            w = max(_MIN_WEIGHT, min(max_w, w))
            weights[voter] = round(w, 4)

        return weights

    # ------------------------------------------------------------------
    # Training path — called by meta_training_loop every 30 min
    # ------------------------------------------------------------------

    def train_step(
        self, feedback_batch: list[dict[str, Any]]
    ) -> dict[str, float]:
        """
        Update voter accuracy EMAs from a batch of labeled voter_feedback rows.

        Each row must contain:
            voter_name      (str)
            voter_signal    ("buy" | "sell" | "hold")
            forward_roc_30s (float | None)  — None rows are skipped

        Returns the updated accuracy dict for logging.
        """
        labeled = [
            r for r in feedback_batch
            if r.get("forward_roc_30s") is not None
        ]
        if not labeled:
            return dict(self._accuracies)

        # Accumulate per-voter correctness within this batch, then EMA.
        batch_correct: dict[str, list[float]] = {}
        for row in labeled:
            voter = row["voter_name"]
            signal = row["voter_signal"]
            roc = float(row["forward_roc_30s"])
            batch_correct.setdefault(voter, []).append(
                float(self._is_correct(signal, roc))
            )

        with self._lock:
            for voter, scores in batch_correct.items():
                batch_mean = sum(scores) / len(scores)
                self._accuracies[voter] = _ema_update(
                    self._accuracies.get(voter, 0.5), batch_mean
                )

        logger.info(
            "MetaTrader: updated %d voter(s) from %d labeled rows.",
            len(batch_correct),
            len(labeled),
        )
        return dict(self._accuracies)

    def _is_correct(self, signal: str, forward_roc: float) -> bool:
        """Return True if the voter signal matched the actual direction."""
        if abs(forward_roc) < self._roc_threshold:
            return signal == "hold"
        if forward_roc > 0:
            return signal == "buy"
        return signal == "sell"


def _ema_update(current: float, new_value: float) -> float:
    """EMA: new = alpha * sample + (1 - alpha) * current."""
    return _EMA_ALPHA * new_value + (1.0 - _EMA_ALPHA) * current


# ---------------------------------------------------------------------------
# Module-level singleton — shared by ensemble_core and meta_training_loop.
# Instantiated once at import time; no heavy work in __init__.
# ---------------------------------------------------------------------------
_instance: MetaTrader | None = None
_instance_lock = threading.Lock()


def get_metatrader() -> MetaTrader:
    """Return the process-wide MetaTrader singleton (lazy init)."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = MetaTrader()
    return _instance
