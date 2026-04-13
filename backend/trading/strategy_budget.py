"""Parse optional per-bot trading budget from strategy_params_json (quote currency)."""
from __future__ import annotations

import json
from typing import Any

# Accepted keys so older saves / manual JSON still work.
_BUDGET_KEYS = (
    "initial_budget_quote",
    "trading_budget_quote",
    "budget_usdt",
)


def initial_budget_from_params_dict(params: dict[str, Any] | None) -> float | None:
    if not params:
        return None
    for key in _BUDGET_KEYS:
        if key not in params:
            continue
        raw = params[key]
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return None


def initial_budget_from_strategy_params_json(raw: str | None) -> float | None:
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return initial_budget_from_params_dict(data)


def parse_initial_budget_api_value(raw: Any) -> float | None:
    """
    Coerce JSON body value for initial_budget_quote.
    None or '' clears; positive numbers stored; 0 clears; negatives invalid.
    """
    if raw is None or raw == "":
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError) as e:
        raise ValueError("initial_budget_quote must be a number or null") from e
    if f < 0:
        raise ValueError("initial_budget_quote must be non-negative")
    return f if f > 0 else None


def merge_strategy_params_json(existing_json: str | None, patch: dict[str, Any]) -> dict[str, Any]:
    base: dict[str, Any] = {}
    if existing_json and existing_json.strip():
        try:
            parsed = json.loads(existing_json)
            if isinstance(parsed, dict):
                base = parsed
        except json.JSONDecodeError:
            base = {}
    merged = {**base, **patch}
    return merged
