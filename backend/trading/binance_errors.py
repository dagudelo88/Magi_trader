"""User-facing explanations for common Binance / ccxt errors."""

from __future__ import annotations

from trading.constants import MODE_LIVE, MODE_TESTNET


def explain_fetch_balance_error(exc: BaseException, execution_mode: str) -> str:
    """Turn raw ccxt/Binance errors into actionable text for the API client."""
    raw = str(exc).strip()
    lines = [raw, "", f"App execution mode is «{execution_mode}» (matches Binance Spot Testnet or Mainnet)."]

    if execution_mode == MODE_TESTNET:
        lines.extend(
            [
                "",
                "You are on TESTNET — keys must come from https://testnet.binance.vision/",
                "In `.env`, prefer BINANCE_TESTNET_API_KEY + BINANCE_TESTNET_API_SECRET; "
                "BINANCE_API_KEY is used for mainnet (Live) only.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "You are on MAINNET — API keys must be from https://www.binance.com/ → API Management.",
                "If you only have testnet keys, switch Settings back to Testnet or create mainnet keys.",
            ]
        )

    if "-2015" in raw or "Invalid API-key" in raw:
        lines.extend(
            [
                "",
                "Binance -2015 usually means one of:",
                "• Wrong key type for this mode (testnet key vs mainnet key).",
                "• IP restriction on the key — add this machine's public IP or disable the restriction for testing.",
                "• Key is missing «Enable Reading» (required for balances).",
                "• Secret/key typo, or stray quotes/spaces/newlines in .env.",
            ]
        )

    return "\n".join(lines)
