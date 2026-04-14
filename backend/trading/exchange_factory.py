from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import ccxt

if TYPE_CHECKING:
    pass


def _spot_credentials(execution_mode: Literal["testnet", "live"]) -> tuple[str, str]:
    """
    Resolve which key pair to use.

    Recommended: set both in `.env` so you never swap files.

    • Testnet mode — prefers `BINANCE_TESTNET_API_KEY` / `BINANCE_TESTNET_API_SECRET`
      (or `BINANCE_TESTNET_SECRET`), otherwise falls back to `BINANCE_*` (single-pair setups).
    • Live mode — always `BINANCE_API_KEY` / `BINANCE_API_SECRET` (or `BINANCE_SECRET`).
    """
    if execution_mode == "testnet":
        k = os.getenv("BINANCE_TESTNET_API_KEY")
        s = os.getenv("BINANCE_TESTNET_API_SECRET") or os.getenv("BINANCE_TESTNET_SECRET")
        if k and s:
            return k, s
        k = os.getenv("BINANCE_API_KEY")
        s = os.getenv("BINANCE_API_SECRET") or os.getenv("BINANCE_SECRET")
        if k and s:
            return k, s
        raise ValueError(
            "Testnet API keys missing. Set BINANCE_TESTNET_API_KEY and "
            "BINANCE_TESTNET_API_SECRET (recommended), or BINANCE_API_KEY and BINANCE_API_SECRET."
        )

    k = os.getenv("BINANCE_API_KEY")
    s = os.getenv("BINANCE_API_SECRET") or os.getenv("BINANCE_SECRET")
    if not k or not s:
        raise ValueError(
            "Mainnet API keys missing. Set BINANCE_API_KEY and BINANCE_API_SECRET for Live mode."
        )
    return k, s


def build_binance_spot(
    execution_mode: Literal["testnet", "live"],
) -> ccxt.binance:
    """
    Authenticated spot exchange instance for balance checks and order execution.
    Testnet uses Binance Spot Testnet (virtual funds).
    Live uses mainnet — only after user confirmation in app_settings.
    """
    api_key, api_secret = _spot_credentials(execution_mode)

    exchange = ccxt.binance(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    if execution_mode == "testnet":
        exchange.set_sandbox_mode(True)
    else:
        exchange.set_sandbox_mode(False)

    return exchange


def build_binance_public() -> ccxt.binance:
    """
    Unauthenticated mainnet exchange for read-only public data (OHLCV, market
    metadata).  No API key required — Binance public REST endpoints are open.

    Use this for signal generation so strategy decisions are based on real
    market prices regardless of the bot's execution_mode (testnet vs live).
    """
    return ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )
