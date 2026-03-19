from __future__ import annotations

import sqlite3
from typing import Any, Literal

from database import get_db_connection
from trading.constants import (
    MODE_LIVE,
    MODE_TESTNET,
    SETTING_EXECUTION_MODE,
    SETTING_GLOBAL_HALT,
)


def _get(cursor: sqlite3.Cursor, key: str) -> str | None:
    cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    return row["value"] if row else None


def get_setting(key: str, default: str | None = None) -> str | None:
    conn = get_db_connection()
    try:
        return _get(conn.cursor(), key) or default
    finally:
        conn.close()


def set_setting(key: str, value: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )
    conn.commit()
    conn.close()


def get_execution_mode() -> Literal["testnet", "live"]:
    raw = get_setting(SETTING_EXECUTION_MODE, MODE_TESTNET)
    return MODE_LIVE if raw == MODE_LIVE else MODE_TESTNET


def is_global_halt() -> bool:
    return (get_setting(SETTING_GLOBAL_HALT, "false") or "false").lower() == "true"


def set_global_halt(halted: bool) -> None:
    set_setting(SETTING_GLOBAL_HALT, "true" if halted else "false")


def apply_execution_mode(mode: str, confirmation_phrase: str | None) -> dict[str, Any]:
    from trading.constants import LIVE_TRADING_CONFIRMATION_PHRASE

    if mode not in (MODE_TESTNET, MODE_LIVE):
        raise ValueError("execution_mode must be 'testnet' or 'live'")

    if mode == MODE_LIVE:
        if (confirmation_phrase or "").strip() != LIVE_TRADING_CONFIRMATION_PHRASE:
            raise ValueError(
                "Live trading requires typing the exact confirmation phrase "
                f"({LIVE_TRADING_CONFIRMATION_PHRASE})"
            )

    set_setting(SETTING_EXECUTION_MODE, mode)
    return trading_settings_snapshot()


def trading_settings_snapshot() -> dict[str, Any]:
    return {
        "execution_mode": get_execution_mode(),
        "global_trading_halted": is_global_halt(),
    }
