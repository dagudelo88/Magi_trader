# Data collection

This document describes how market tick data is gathered, what gets stored, and how it fits into the rest of the MagiTrader backend.

## Overview

The app runs a **continuous Binance spot WebSocket client** that maintains in-memory state for a fixed list of USDT pairs. Once per second it **snapshots** that state into SQLite (`market_ticks`). The design targets **lead–lag style features** (BTC moves vs. each “target” asset) and **per-second volume change**, with a JSON column for extended fields used in ML-oriented workflows.

Execution mode (testnet vs. live) affects **trading** via `app_settings` and CCXT; it does **not** switch the collector to testnet. Price streams always use **Binance mainnet** public WebSockets so recorded microstructure matches real markets.

## Where the collector runs

- **Module:** `backend/services/data_collector.py`
- **Lifecycle:** Started as an `asyncio` task in the FastAPI app lifespan (`backend/main.py`), alongside the bot runner. It is always on while the API process is running.
- **Status endpoint:** `GET /api/data/status` reports that the collector is managed in-process (`active` / `managed`).

## Which markets are tracked

The list is defined in **`backend/tracked_markets.py`** as `TRACKED_USDT_STREAM_IDS` (eight Binance-style lowercase symbols without a slash, e.g. `btcusdt`). The same list drives:

- WebSocket subscription set
- `GET /api/market/tracked` (stream IDs, ticker symbols, CCXT symbols)
- Testnet wallet filtering (bases aligned with these pairs)

To change coverage, edit that module so collectors, dashboard tickers, and wallet views stay consistent.

## How data is gathered

### WebSocket streams

The collector opens a **combined stream** to `wss://stream.binance.com:9443/stream?streams=...` with **two channels per symbol**:

| Stream suffix   | Purpose |
|----------------|---------|
| `@ticker`      | Last price (`c`), 24h rolling volume (`v`) |
| `@bookTicker`  | Best bid (`b`), best ask (`a`) → mid and **spread in basis points** |

Messages are parsed and merged into a **per-symbol in-memory state** (`state` dict): rolling price history (deque, up to 120 seconds), latest bid/ask/spread, 24h volume and previous volume for delta computation.

On disconnect, the client **reconnects** with exponential backoff (capped), then resumes.

### One-second logging loop

A separate coroutine (`_data_logger`):

1. Waits a short startup delay so streams can populate.
2. Every **1 second**:
   - Appends each symbol’s latest trade price into its rolling deque (1 sample per second).
   - Calls `_write_ticks`, which inserts rows into `market_ticks`.

If BTC has no usable price yet, the writer skips that cycle (alts need a valid BTC reference).

### Computed fields

- **ROC (rate of change):** For BTC and each target, over windows **1, 5, 10, 30, 60** seconds (each deque slot ≈ 1 s). The table stores **1s and 5s** ROC in dedicated columns; **all windows** are also embedded in `features_json` for targets and BTC.
- **Volume delta:** Derived from the **24h rolling volume** field in the ticker: difference from the previous second (`max(0, current - previous)`). The first sample after startup yields **0**; a day boundary reset is treated as a non-negative step.
- **Spread:** From bid/ask on the target asset; BTC spread and BTC bid/ask appear inside `features_json` for context.

## How data is stored

### Database and file

- **Engine:** SQLite 3  
- **Path:** `data/magitrader.db` (directory created if missing; path resolved from `backend/database.py` as `DB_PATH`).

### Table: `market_ticks`

Created in `init_db()` in `backend/database.py`. This is the **only table the collector writes to** in the current codebase.

| Column | Meaning |
|--------|---------|
| `tick_id` | Auto-increment primary key |
| `timestamp` | Unix time in **milliseconds** (wall-clock at snapshot) |
| `target_asset` | CCXT-style symbol, e.g. `ETH/USDT` (from `stream_id_to_ccxt`) |
| `target_price` | Last price for the target at snapshot time |
| `btc_price` | BTC/USDT price at the same snapshot |
| `btc_roc_1s`, `btc_roc_5s` | BTC ROC |
| `target_roc_1s`, `target_roc_5s` | Target ROC |
| `btc_volume_delta` | Per-second BTC volume delta |
| `target_volume_delta` | Per-second target volume delta |
| `spread_bps` | Target asset spread (basis points) |
| `features_json` | JSON object: bid/ask, BTC bid/ask, `btc_spread_bps`, and extended `target_roc_*` / `btc_roc_*` for all ROC windows |

Indexes: `timestamp`, `target_asset` (for time-series and per-asset queries).

### Related tables (not filled by the collector)

| Table | Role |
|-------|------|
| `market_depth` | Schema for optional order-book snapshots keyed by `tick_id`. **No current writer** in the repository; reserved for future use. |
| `bot_decisions` | When the bot runner records a decision, it can link to the **latest** `market_ticks` row for that symbol (`get_latest_tick_id` in `database.py`). |
| `bots`, `bot_logs`, `bot_orders`, `app_settings` | Trading, logging, and settings—orthogonal to tick ingestion but part of the same DB file. |

### API surface for storage visibility

- `GET /api/db/stats` — file size and row counts per table (used by the frontend Database page).

## What is *not* stored in `market_ticks`

- **OHLCV candles** for charts are fetched on demand via CCXT (`GET /api/market/ohlcv`) from the exchange for the configured execution mode; they are **not** the same pipeline as `market_ticks`.
- **Full order books** are not persisted unless you implement writers for `market_depth`.

## Operational notes

- **Rate of inserts:** Up to **eight rows per second** (one per tracked pair), each insert batch uses `executemany` in a single transaction.
- **Stopping the server** cancels the collector task gracefully (`asyncio.CancelledError`).

For implementation details, see `backend/services/data_collector.py` and the `CREATE TABLE` / `init_db()` section of `backend/database.py`.
