"""
Single source of truth for spot pairs we follow on Binance streams and testnet/wallet views.

Stream IDs: lowercase, no slash (Binance combined stream format).
"""

from __future__ import annotations

# Eight USDT spot pairs — keep WebSocket collectors, dashboard tickers, and balance filtering aligned.
TRACKED_USDT_STREAM_IDS: tuple[str, ...] = (
    "btcusdt",
    "ethusdt",
    "bnbusdt",
    "solusdt",
    "xrpusdt",
    "adausdt",
    "dogeusdt",
    "avaxusdt",
)

BTC_STREAM_ID = "btcusdt"


def stream_id_to_ccxt(stream_lower: str) -> str:
    s = stream_lower.lower()
    if not s.endswith("usdt"):
        raise ValueError(f"Expected *usdt stream id, got: {stream_lower!r}")
    base = s[: -len("usdt")].upper()
    return f"{base}/USDT"


def stream_id_to_ticker_symbol(stream_lower: str) -> str:
    """e.g. btcusdt -> BTCUSDT (Binance miniTicker field `s`)."""
    return stream_lower.upper()


TRACKED_CCXT_SYMBOLS: tuple[str, ...] = tuple(
    stream_id_to_ccxt(s) for s in TRACKED_USDT_STREAM_IDS
)

TRACKED_BASE_ASSETS: frozenset[str] = frozenset(
    stream_id_to_ccxt(s).split("/")[0] for s in TRACKED_USDT_STREAM_IDS
)

TRACKED_BASE_ORDER: tuple[str, ...] = tuple(
    stream_id_to_ccxt(s).split("/")[0] for s in TRACKED_USDT_STREAM_IDS
)

# Shown in wallet alongside the eight bases (valuation / bots).
TRACKED_WALLET_EXTRA_ASSETS: frozenset[str] = frozenset(
    ("USDT", "USDC", "FDUSD", "BUSD")
)


def alt_stream_ids() -> tuple[str, ...]:
    return tuple(s for s in TRACKED_USDT_STREAM_IDS if s != BTC_STREAM_ID)


def wallet_assets_allowed() -> frozenset[str]:
    return TRACKED_BASE_ASSETS | TRACKED_WALLET_EXTRA_ASSETS
