# Data Collection

This document describes how market data is gathered, what gets stored, and how it integrates with the MagiTrader trading and machine-learning stack.

## Overview

MagiTrader runs a **continuous Binance spot WebSocket client** that maintains in-memory state for a fixed list of USDT pairs. Once per second it **snapshots** that state into SQLite (`market_ticks`). The design targets **lead–lag features** (BTC moves vs. each "target" altcoin) and **per-second volume change**, with a JSON column for extended ML fields.

In addition, every time the bot runner fetches OHLCV candles from Binance for a strategy decision, those candles are **persisted to `ohlcv_candles`**. This dual-track storage enables full historical replay and backtesting without any further API calls.

Execution mode (`testnet` vs. `live`) affects **trading** via `app_settings` and CCXT. It does **not** switch the collector to testnet — price streams always use **Binance mainnet** public WebSockets so recorded microstructure reflects real markets.

---

## Where the collector runs

- **Module:** `backend/services/data_collector.py`
- **Lifecycle:** Started as an `asyncio` task in the FastAPI app lifespan (`backend/main.py`), alongside the bot runner. Always on while the API process is running.
- **Status endpoint:** `GET /api/data/status` reports that the collector is managed in-process (`active` / `managed`).

---

## Tracked markets

The list is defined in **`backend/tracked_markets.py`** as `TRACKED_USDT_STREAM_IDS` (eight Binance-style lowercase stream IDs, e.g. `btcusdt`):

| CCXT Symbol | Description |
|---|---|
| `BTC/USDT` | Bitcoin — also the **lead asset** for all lag features |
| `ETH/USDT` | Ethereum |
| `BNB/USDT` | BNB |
| `SOL/USDT` | Solana |
| `XRP/USDT` | XRP |
| `ADA/USDT` | Cardano |
| `DOGE/USDT` | Dogecoin |
| `AVAX/USDT` | Avalanche |

This same list drives the WebSocket subscription set, `GET /api/market/tracked`, and testnet wallet filtering. To change coverage, edit only `tracked_markets.py`.

---

## How tick data is gathered

### WebSocket streams

`data_collector.py` opens a **combined stream** to `wss://stream.binance.com:9443/stream?streams=…` with two channels per symbol:

| Stream suffix | Purpose |
|---|---|
| `@ticker` | Last price (`c`), 24 h rolling volume (`v`) |
| `@bookTicker` | Best bid (`b`), best ask (`a`) → mid-price and **spread in basis points** |

Messages merge into a per-symbol in-memory `state` dict: a rolling price deque (120 s), latest bid/ask/spread, 24 h volume and the previous value for delta computation. On disconnect the client reconnects with exponential backoff (2 s → 60 s cap).

### One-second snapshot loop

A separate coroutine (`_data_logger`) runs every **1 second**:

1. Appends each symbol's latest trade price to its rolling deque.
2. Calls `_write_ticks`, which inserts one row per symbol into `market_ticks`.

If BTC has no usable price yet, the writer skips the cycle (altcoin rows require a valid BTC reference).

### Computed fields

| Field | Computation |
|---|---|
| **ROC** | Rate of change over windows **1, 5, 10, 30, 60 s**. The table stores `1s` and `5s` in dedicated columns; all five windows are also stored in `features_json`. |
| **Volume delta** | `max(0, volume_24h_now − volume_24h_prev)`. First sample after startup is 0; day-boundary resets are treated as non-negative. |
| **Spread** | `(ask − bid) / mid × 10 000` in basis points. BTC spread and BTC bid/ask also embedded in `features_json`. |

---

## Database

- **Engine:** SQLite 3, **WAL journal mode** (`PRAGMA journal_mode=WAL`). WAL allows concurrent readers while a writer is active — essential since the data collector (1 Hz writes) and bot runner (strategy reads) access the DB simultaneously.
- **Synchronous mode:** `NORMAL` (flush-on-checkpoint, not every write).
- **Path:** `data/magitrader.db` (directory created on first run; path resolved in `backend/database.py` as `DB_PATH`).

WAL mode is set once on startup by `_enable_wal_once()` in `database.py` and persists in the file. Subsequent connections use it automatically.

---

## Tables

### `market_ticks` — written by `data_collector`

One row per symbol per second. The only table the collector writes to.

| Column | Meaning |
|---|---|
| `tick_id` | Auto-increment primary key |
| `timestamp` | Unix time in **milliseconds** |
| `target_asset` | CCXT symbol, e.g. `ETH/USDT` |
| `target_price` | Last price of the target at snapshot time |
| `btc_price` | BTC/USDT price at the same snapshot |
| `btc_roc_1s`, `btc_roc_5s` | BTC rate of change |
| `target_roc_1s`, `target_roc_5s` | Target asset rate of change |
| `btc_volume_delta` | Per-second BTC volume delta |
| `target_volume_delta` | Per-second target volume delta |
| `spread_bps` | Target asset bid-ask spread (basis points) |
| `features_json` | JSON blob: bid/ask, BTC bid/ask, `btc_spread_bps`, and extended `target_roc_*` / `btc_roc_*` for all 5 ROC windows |

Indexes: `timestamp`, `target_asset`.

**Lag ensemble usage:** `backend/trading/strategies/lag_helpers.get_latest_lag_features()` queries this table for the most recent N seconds of BTC and altcoin prices/ROCs. It accepts an optional `as_of_ts` timestamp so the backtesting engine can query the exact snapshot that existed at any historical moment.

---

### `ohlcv_candles` — written by `bot_runner`

CCXT-format OHLCV candles persisted each time the bot runner fetches from Binance. Provides the historical dataset consumed by the backtesting engine.

| Column | Meaning |
|---|---|
| `id` | Auto-increment primary key |
| `symbol` | CCXT symbol, e.g. `BTC/USDT` |
| `timeframe` | Candle duration string, e.g. `1m`, `5m`, `1h` |
| `ts_open` | Candle open time in **milliseconds** |
| `open`, `high`, `low`, `close`, `volume` | Standard OHLCV fields |

Unique constraint: `(symbol, timeframe, ts_open)` — duplicate candles from repeated fetches are silently ignored (`INSERT OR IGNORE`).

Index: `(symbol, timeframe, ts_open)`.

**Write path:** In `bot_runner._run_one_cycle()`, immediately after each `exchange.fetch_ohlcv()` call, candles are passed to `database.upsert_ohlcv_candles()`. This is fire-and-forget — failures are swallowed so the live trading path is never interrupted.

**Backfilling:** To seed historical data before the live bot has run, use:

```bash
python scripts/seed_ohlcv.py --all --timeframe 1m 5m 15m --days 30
```

This paginates the Binance public REST API (no API key required) and inserts into `ohlcv_candles`.

---

### `voter_feedback` — written by `bot_runner`

One row per voter per ensemble decision cycle. Powers MetaMagi dynamic weight learning. Rows include `execution_mode` so labels, exports, and weight updates can be audited separately for testnet and live.

| Column | Meaning |
|---|---|
| `feedback_id` | Auto-increment primary key |
| `bot_id` | Foreign key to `bots.bot_id` |
| `timestamp` | Unix time in **milliseconds** (decision time) |
| `target_asset` | CCXT symbol |
| `ensemble_signal` | Final consensus output (`buy` / `sell` / `hold`) |
| `voter_name` | Registry name of the individual voter (e.g. `macd_rsi`) |
| `voter_signal` | That voter's raw signal (`buy` / `sell` / `hold`) |
| `confidence` | Voter-level confidence score (0–1), if the voter returns one |
| `consensus_score` | Weighted fraction behind the winning signal at decision time |
| `features_snapshot` | JSON copy of the latest `market_ticks.features_json` at decision time — the primary feature vector for MetaMagi neural-net training |
| `forward_roc_30s` | Actual price ROC 30 s after the decision — filled later by the labeling loop |
| `forward_roc_5m` | Actual price ROC 5 min after the decision — filled later by the labeling loop |
| `realized_pnl` | P&L from the executed trade (NULL if signal was `hold` or trade not yet closed) |

Indexes: `(target_asset, timestamp)`, `(bot_id, timestamp)`.

---

### Other tables (not filled by data collection)

| Table | Role |
|---|---|
| `bots` | Bot configurations (strategy, symbol, params, status) |
| `bot_decisions` | High-level buy/sell/hold decisions with confidence |
| `bot_orders` | Exchange order records (CCXT response payloads) |
| `bot_logs` | Structured log lines per bot per cycle |
| `app_settings` | Key-value store for runtime settings (`execution_mode`, `global_trading_halted`) |
| `market_depth` | Schema for optional order-book snapshots keyed by `tick_id`. No current writer — reserved for future use. |

---

## Forward-return labeling (MetaMagi)

A background task (`_meta_training_loop` in `backend/main.py`) runs every **30 minutes** and calls `label_voter_feedback_forward_roc()` from `database.py`. This function updates all unlabeled `voter_feedback` rows within the last **48 hours** using two single-pass SQL `UPDATE` statements with correlated subqueries — one for `forward_roc_30s`, one for `forward_roc_5m`. No Binance API calls are made; it is a pure SQLite self-join against `market_ticks`.

Because `market_ticks` stores one snapshot per second per asset continuously, forward-return data is available within 5–10 minutes of any decision.

---

## MetaMagi weight learning

MetaMagi keeps feedback mode-aware, but live bots are not forced to start cold: live dynamic weights use testnet-learned accuracy as the prior and then overlay live feedback once real mainnet rows exist. Manual optimization can also target a mode with `POST /api/bots/{bot_id}/optimize-weights?mode=testnet|live|current`.

After labeling, the background loop calls `metatrader.train_step(batch)` in `backend/trading/metatrader.py`. This updates each voter's **exponential moving accuracy** — the fraction of past decisions where the voter's signal matched the actual forward price direction. On the next bot cycle, `ensemble_core.run_consensus()` calls `metatrader.get_dynamic_weights()` to shift voter weights toward historically accurate voters.

The training loop is entirely local — no network calls, no interference with tick ingestion.

---

## Backtesting

The backtesting infrastructure replays stored data through any registered strategy without any live API calls:

| Component | Location | Purpose |
|---|---|---|
| `BacktestEngine` | `backend/backtesting/engine.py` | Iterates `ohlcv_candles`, slices lookback windows, calls `strategy.evaluate()`, simulates trades with fees, returns P&L metrics |
| `run_backtest.py` | `scripts/run_backtest.py` | CLI: backtest by bot ID, strategy name, or all bots at once |
| `seed_ohlcv.py` | `scripts/seed_ohlcv.py` | One-time historical seeder via Binance REST |
| `forward_roc_analysis.py` | `scripts/forward_roc_analysis.py` | Statistical analysis of labeled voter signals and directional accuracy |

Lag ensemble strategies pass `as_of_ts` from the engine into `lag_helpers.get_latest_lag_features()`, so market-tick lookups are correctly time-bounded to the historical window being replayed.

---

## Operational notes

- **Insert rate:** Up to **8 rows/second** (one per tracked pair); each batch uses `executemany` in a single transaction.
- **Disk growth:** ~5 MB/day for `market_ticks` at 8 symbols × 1 Hz; `ohlcv_candles` grows at the bot polling frequency (5 s cycle, 1–3 candle timeframes per bot).
- **Stopping the server:** Cancels the collector task gracefully (`asyncio.CancelledError`).

For implementation details see `backend/services/data_collector.py`, `backend/services/bot_runner.py`, and the `init_db()` section of `backend/database.py`.
