from __future__ import annotations

import json
from typing import Any

from database import get_bot_risk_settings, upsert_bot_risk_settings
from trading.app_settings import get_setting, set_setting

SETTING_RISK_DEFAULTS = "risk_defaults"

DEFAULT_DYNAMIC_TIERS: list[dict[str, float | None]] = [
    {"min_score": None, "max_score": 0.40, "multiplier": 0.50},
    {"min_score": 0.40, "max_score": 0.70, "multiplier": 1.00},
    {"min_score": 0.70, "max_score": 0.85, "multiplier": 1.40},
    {"min_score": 0.85, "max_score": None, "multiplier": 1.75},
]

DEFAULT_RISK_SETTINGS: dict[str, Any] = {
    "base_risk_pct": 2.0,
    "dynamic_tiers": DEFAULT_DYNAMIC_TIERS,
    "daily_loss_limit_pct": 6.0,
    "max_drawdown_pct": 15.0,
    "consecutive_loss_limit": 8,
    "enable_daily_loss_limit": True,
    "enable_drawdown_protection": True,
    "enable_consecutive_loss": True,
    "enable_dynamic_sizing": True,
    "enable_volatility_pause": False,
    "volatility_threshold": None,
    "drawdown_action": "reduce",
    "drawdown_reduce_factor": 0.5,
    "yolo_mode": False,
}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_float(value: Any, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_tiers(value: Any) -> list[dict[str, float | None]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if not isinstance(value, list):
        return [dict(tier) for tier in DEFAULT_DYNAMIC_TIERS]

    tiers: list[dict[str, float | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        min_score = _as_float(item.get("min_score"), None)
        max_score = _as_float(item.get("max_score"), None)
        multiplier = _as_float(item.get("multiplier"), 1.0)
        if multiplier is None or multiplier <= 0:
            continue
        if min_score is not None and (min_score < 0 or min_score > 1):
            continue
        if max_score is not None and (max_score < 0 or max_score > 1):
            continue
        if (
            min_score is not None
            and max_score is not None
            and min_score >= max_score
        ):
            continue
        tiers.append(
            {
                "min_score": min_score,
                "max_score": max_score,
                "multiplier": multiplier,
            }
        )
    return tiers or [dict(tier) for tier in DEFAULT_DYNAMIC_TIERS]


def normalize_risk_settings(
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = {**DEFAULT_RISK_SETTINGS, **(raw or {})}
    base_risk_pct = _as_float(
        source.get("base_risk_pct"),
        DEFAULT_RISK_SETTINGS["base_risk_pct"],
    )
    dynamic_tiers = _parse_tiers(
        source.get("dynamic_tiers", source.get("dynamic_tiers_json"))
    )
    daily_loss_limit_pct = _as_float(
        source.get("daily_loss_limit_pct"),
        DEFAULT_RISK_SETTINGS["daily_loss_limit_pct"],
    )
    max_drawdown_pct = _as_float(
        source.get("max_drawdown_pct"),
        DEFAULT_RISK_SETTINGS["max_drawdown_pct"],
    )
    consecutive_loss_limit = _as_int(
        source.get("consecutive_loss_limit"),
        DEFAULT_RISK_SETTINGS["consecutive_loss_limit"],
    )
    volatility_threshold = _as_float(source.get("volatility_threshold"), None)
    drawdown_action = str(source.get("drawdown_action") or "reduce")
    drawdown_reduce_factor = _as_float(
        source.get("drawdown_reduce_factor"),
        0.5,
    )

    if base_risk_pct is None or base_risk_pct <= 0:
        raise ValueError("base_risk_pct must be positive")
    if daily_loss_limit_pct is None or daily_loss_limit_pct <= 0:
        raise ValueError("daily_loss_limit_pct must be positive")
    if max_drawdown_pct is None or max_drawdown_pct <= 0:
        raise ValueError("max_drawdown_pct must be positive")
    if consecutive_loss_limit <= 0:
        raise ValueError("consecutive_loss_limit must be positive")
    if drawdown_action not in {"reduce", "pause"}:
        raise ValueError("drawdown_action must be 'reduce' or 'pause'")
    if drawdown_reduce_factor is None or not (0 < drawdown_reduce_factor <= 1):
        raise ValueError("drawdown_reduce_factor must be > 0 and <= 1")
    if volatility_threshold is not None and volatility_threshold <= 0:
        raise ValueError("volatility_threshold must be positive when set")
    return {
        "base_risk_pct": base_risk_pct,
        "dynamic_tiers": dynamic_tiers,
        "daily_loss_limit_pct": daily_loss_limit_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "consecutive_loss_limit": consecutive_loss_limit,
        "enable_daily_loss_limit": _as_bool(
            source.get("enable_daily_loss_limit"),
            DEFAULT_RISK_SETTINGS["enable_daily_loss_limit"],
        ),
        "enable_drawdown_protection": _as_bool(
            source.get("enable_drawdown_protection"),
            DEFAULT_RISK_SETTINGS["enable_drawdown_protection"],
        ),
        "enable_consecutive_loss": _as_bool(
            source.get("enable_consecutive_loss"),
            DEFAULT_RISK_SETTINGS["enable_consecutive_loss"],
        ),
        "enable_dynamic_sizing": _as_bool(
            source.get("enable_dynamic_sizing"),
            DEFAULT_RISK_SETTINGS["enable_dynamic_sizing"],
        ),
        "enable_volatility_pause": _as_bool(
            source.get("enable_volatility_pause"),
            DEFAULT_RISK_SETTINGS["enable_volatility_pause"],
        ),
        "volatility_threshold": volatility_threshold,
        "drawdown_action": drawdown_action,
        "drawdown_reduce_factor": drawdown_reduce_factor,
        "yolo_mode": _as_bool(
            source.get("yolo_mode"),
            DEFAULT_RISK_SETTINGS["yolo_mode"],
        ),
    }


def db_row_to_risk_settings(
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not row:
        return None
    raw = dict(row)
    raw["dynamic_tiers"] = raw.pop("dynamic_tiers_json", None)
    return normalize_risk_settings(raw)


def get_global_risk_defaults() -> dict[str, Any]:
    raw = get_setting(SETTING_RISK_DEFAULTS)
    if not raw:
        return normalize_risk_settings()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return normalize_risk_settings()
    parsed_dict = parsed if isinstance(parsed, dict) else None
    return normalize_risk_settings(parsed_dict)


def set_global_risk_defaults(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_risk_settings(settings)
    set_setting(SETTING_RISK_DEFAULTS, json.dumps(normalized, default=str))
    return normalized


def template_risk_defaults(strategy_name: str) -> dict[str, Any]:
    defaults = get_global_risk_defaults()
    is_lag = strategy_name.startswith("magi_lag_")
    if strategy_name.endswith("_high"):
        profile = {
            "base_risk_pct": 1.5,
            "daily_loss_limit_pct": 5.0,
            "max_drawdown_pct": 12.0,
            "consecutive_loss_limit": 10,
        }
    elif strategy_name.endswith("_low"):
        profile = {
            "base_risk_pct": 2.8,
            "daily_loss_limit_pct": 8.0,
            "max_drawdown_pct": 18.0,
            "consecutive_loss_limit": 6,
        }
    else:
        profile = {
            "base_risk_pct": 2.0,
            "daily_loss_limit_pct": 6.0,
            "max_drawdown_pct": 15.0,
            "consecutive_loss_limit": 8,
        }
    if is_lag:
        profile["base_risk_pct"] = round(
            float(profile["base_risk_pct"]) * 0.85,
            4,
        )
        profile["daily_loss_limit_pct"] = max(
            1.0,
            round(float(profile["daily_loss_limit_pct"]) - 1.0, 4),
        )
        profile["max_drawdown_pct"] = max(
            1.0,
            round(float(profile["max_drawdown_pct"]) - 2.0, 4),
        )
    return normalize_risk_settings({**defaults, **profile})


def get_effective_bot_risk_settings(bot_id: str) -> dict[str, Any]:
    row = get_bot_risk_settings(bot_id)
    return db_row_to_risk_settings(row) or get_global_risk_defaults()


def ensure_bot_risk_settings(bot_id: str) -> dict[str, Any]:
    row = get_bot_risk_settings(bot_id)
    if row:
        return db_row_to_risk_settings(row) or get_global_risk_defaults()
    return save_bot_risk_settings(bot_id, get_global_risk_defaults())


def save_bot_risk_settings(
    bot_id: str,
    settings: dict[str, Any],
) -> dict[str, Any]:
    normalized = normalize_risk_settings(settings)
    row = upsert_bot_risk_settings(bot_id, normalized)
    return db_row_to_risk_settings(row) or normalized
