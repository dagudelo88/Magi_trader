"""Central literals for trading — avoid scattering magic strings."""

# Keys in app_settings
SETTING_EXECUTION_MODE = "execution_mode"
SETTING_GLOBAL_HALT = "global_trading_halted"

# execution_mode values
MODE_TESTNET = "testnet"
MODE_LIVE = "live"

# User must type this (exactly) to switch API routing to Binance mainnet + real balances.
LIVE_TRADING_CONFIRMATION_PHRASE = "ENABLE LIVE TRADING"
