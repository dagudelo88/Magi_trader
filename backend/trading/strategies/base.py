"""Shared contract for all MagiTrader trading strategies."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class SignalResult:
    """
    Unified return type for every strategy's evaluate() function.

    signal     – trading decision for this bar.
    meta       – strategy-specific indicator values for logging / UI display.
    close_count – number of closes fed to the strategy (for warmup messages).
    warmup     – True while the strategy lacks enough bars to produce a real signal.
    confidence – optional 0.0–1.0 quality score (used in bot_decisions table).
    """

    signal: Literal["buy", "sell", "hold"]
    meta: dict[str, Any] = field(default_factory=dict)
    close_count: int = 0
    warmup: bool = False
    confidence: float | None = None
