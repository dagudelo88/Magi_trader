# Data Collection

This document describes how market data is gathered, what gets stored, how it is written (batching, pooling), and how **labeled** MetaMagi training rows are produced from SQLite.

## Overview

MagiTrader runs a **continuous Binance spot WebSocket client** that keeps in-memory state for a fixed list of USDT pairs. Every **second** it extends an in-memory snapshot; those snapshots are **batched** and flushed to SQLite (`market_ticks`) on a configurable interval (default **10 seconds**). The design targets **lead–lag features** (BTC vs. each alt), **per-second volume change** derived from Binance’s rolling 24 h volume field, and a JSON column for extended ML fields.

Whenever the bot runner fetches OHLCV from Binance for a strategy, those candles are **persisted to `ohlcv_candles`**. Ensemble bots also write **`voter_feedback`** (one row per voter per cycle) with a copy of the latest **`market_ticks.features_json`** for that symbol. Forward-return columns on `voter_feedback` are filled asynchronously by a background loop using only **`market_ticks`** (no extra API calls).

Execution mode (`testnet` vs. `live`) affects **trading** via `app_settings` and CCXT. It does **not** switch the collector to testnet — price streams always use **Binance mainnet** public WebSockets.

---

## Where the collector runs

- **Module:** `backend/services/data_collector.py`
- **Lifecycle:** Started as an `asyncio` task in the FastAPI app lifespan (`backend/main.py`), alongside the bot runner. Active while the API process runs.
- **Status endpoint:** `GET /api/data/status` reports in-process collector state.

---

## Tracked markets

Single source: **`backend/tracked_markets.py`** — `TRACKED_USDT_STREAM_IDS` (eight Binance-style lowercase stream IDs, e.g. `btcusdt`):

| CCXT Symbol | Notes |
|---|---|
| `BTC/USDT` | **Lead asset** for lag-style features on every row |
| `ETH/USDT` | |
| `BNB/USDT` | |
| `SOL/USDT` | |
| `XRP/USDT` | |
| `ADA/USDT` | |
| `DOGE/USDT` | |
| `AVAX/USDT` | |

This list drives the WebSocket subscription set, `GET /api/market/tracked`, and testnet wallet filtering. Change coverage only in `tracked_markets.py`.

---

## How tick data is gathered

### WebSocket streams

`data_collector.py` opens one or more **combined** connections to `wss://stream.binance.com:9443/stream?streams=…` (chunks if ever needed for the 1024-stream limit). For each tracked symbol it subscribes to:

| Stream suffix | Purpose |
|---|---|
| `@ticker` | Last price (`c`), rolling 24 h base volume (`v`) |
| `@bookTicker` | Best bid (`b`), best ask (`a`) → mid and **spread (basis points)** |

Messages merge into per-symbol in-memory `state`: a 120-sample price deque (1 Hz sampling), latest bid/ask/spread, 24 h volume and `prev_volume_24h` for delta computation. Connections use **proactive recycle** before Binance’s ~24 h cap, receive timeouts, and exponential reconnect backoff (2 s → 60 s cap).

### One-second snapshot loop (in-memory → batched DB flush)

`_data_logger` wakes every **1 second**:

1. Appends each symbol’s `latest_price` from the WebSocket into its rolling deque (ROC windows 1, 5, 10, 30, 60 s).
2. Builds up to one logical row per tracked symbol via `_build_tick_rows` and appends to a **`pending_rows` buffer** (no DB write yet).
3. After **`TICK_BATCH_SEC`** seconds (default **10**, env `TICK_BATCH_SEC`), calls `_flush_ticks` — a single `executemany` `INSERT` + `commit`. On repeated flush failure, pending rows are capped by **`MAX_PENDING_TICK_ROWS`** (default 2000) with oldest drops logged.

If BTC has no usable price yet, that cycle produces no rows (alt rows require a valid BTC reference).

### Volume delta

**`btc_volume_delta`** / **`target_volume_delta`** are computed from the **change in Binance’s 24 h rolling ticker volume** between consecutive 1 Hz snapshots: `max(0, current − previous)`, with the first sample after startup as 0. This is **not** exchange “per-second traded volume”; it is a stable, cheap proxy aligned with the public ticker stream.

### Computed fields

| Field | Computation |
|---|---|
| **ROC (1s / 5s columns)** | Rate of change over the rolling deque for windows **1** and **5** seconds (`btc_roc_*`, `target_roc_*` main columns). |
| **ROC (all windows in JSON)** | Windows **1, 5, 10, 30, 60** s appear in `features_json` as `target_roc_{n}s` and `btc_roc_{n}s`. |
| **Spread** | `(ask − bid) / mid × 10 000` (bps), stored per row in `spread_bps`; BTC spread also inside `features_json` as `btc_spread_bps`. |

### `features_json` shape (per row)

Serialized JSON string, same keys the snapshot builder writes today:

- `bid`, `ask` (target)
- `btc_bid`, `btc_ask`, `btc_spread_bps`
- `target_roc_1s` … `target_roc_60s`
- `btc_roc_1s` … `btc_roc_60s`

**`voter_feedback.features_snapshot`** is the **latest** `features_json` for the bot’s symbol at decision time (see `bot_runner._get_features_snapshot()`).

---

## Database

- **Engine:** SQLite 3, **WAL** (`PRAGMA journal_mode=WAL`), set once via `_enable_wal_once()` in `backend/database.py`.
- **Path:** `data/magitrader.db`.
- **Access pattern:** **`get_db_connection()`** borrows from a **thread-safe pool** (size `DB_POOL_SIZE`, default 10). Pool connections use `synchronous=NORMAL`, `cache_size=10000`, `temp_store=MEMORY`, and `busy_timeout` (default 15 s). MetaMagi labeling uses **`get_direct_db_connection()`** with a **short** busy timeout so it yields under writer contention.
- **Writes:** Hot paths batch operations (`executemany`) and use **`_retry_sqlite_write`** for transient SQLITE_BUSY.

---

## Tables directly involved in “data collection”

### `market_ticks` — WebSocket collector

One row per tracked symbol per **second** (logical); **physical** inserts happen in batches of **`TICK_BATCH_SEC` seconds** of accumulated rows.

| Column | Meaning |
|---|---|
| `tick_id` | INTEGER PRIMARY KEY AUTOINCREMENT |
| `timestamp` | Unix time **milliseconds** (snapshot wall clock) |
| `target_asset` | CCXT symbol, e.g. `ETH/USDT` |
| `target_price` | Last price of the target |
| `btc_price` | `BTC/USDT` last at same snapshot |
| `btc_roc_1s`, `btc_roc_5s` | BTC ROC (main table mirrors) |
| `target_roc_1s`, `target_roc_5s` | Target ROC (main table mirrors) |
| `btc_volume_delta` | Delta from rolling 24 h volume field (see above) |
| `target_volume_delta` | Same for target |
| `spread_bps` | Target bid–ask spread (bps) |
| `features_json` | JSON with bid/ask, BTC microstructure, all ROC windows |

**Indexes:** `idx_market_ticks_timestamp`, `idx_market_ticks_asset`, **`idx_market_ticks_asset_ts`** `(target_asset, timestamp)`.

**Consumers:** `backend/trading/strategies/lag_helpers.get_latest_lag_features()` (optional `as_of_ts` for backtests).

---

### `ohlcv_candles` — bot runner (CCXT)

| Column | Meaning |
|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT |
| `symbol`, `timeframe` | CCXT symbol and interval string |
| `ts_open` | Candle open **ms** |
| `open`, `high`, `low`, `close`, `volume` | OHLCV |

**Constraint / index:** `UNIQUE(symbol, timeframe, ts_open)`; inserts use `INSERT OR IGNORE`.

**Write path:** After `exchange.fetch_ohlcv()` in the trading loop → `database.upsert_ohlcv_candles()`.

**Backfill:** `python scripts/seed_ohlcv.py --all --timeframe 1m 5m 15m --days 30`

---

### `voter_feedback` — ensemble bot runner (MetaMagi grain)

**Grain:** one row **per voter** per ensemble **decision cycle** for a bot (shared `timestamp`, `ensemble_signal`, `target_asset` within that cycle).

| Column | Meaning |
|---|---|
| `feedback_id` | INTEGER PRIMARY KEY AUTOINCREMENT |
| `bot_id` | Bot that produced the vote (nullable on legacy rows) |
| `execution_mode` | `testnet` \| `live` (defaults applied on insert) |
| `timestamp` | Decision time **ms** |
| `target_asset` | CCXT symbol |
| `ensemble_signal` | Consensus: `buy` \| `sell` \| `hold` |
| `voter_name` | Registry name, e.g. `macd_rsi` |
| `voter_signal` | That voter’s signal |
| `confidence` | Voter confidence 0–1 when provided |
| `consensus_score` | Weighted fraction behind winning signal (from ensemble meta) |
| `features_snapshot` | **`features_json`** copy from latest `market_ticks` for `target_asset` |
| `forward_roc_30s` | **Labeled** forward return (see below); NULL until labeling runs |
| `forward_roc_5m` | **Labeled** forward return (5 min horizon) |
| `realized_pnl` | **Reserved** for trade-outcome linkage; **not** populated by the current labeling SQL (almost always NULL in practice; see `scripts/bot_strategy_report.py` note) |

**Indexes:** `(target_asset, timestamp)`, `(bot_id, timestamp)`, **`(bot_id, execution_mode, timestamp)`**, **`(voter_name, timestamp)`**, plus a partial unlabeled-row index used by MetaMagi catch-up.

**Flush path:** `_log_voter_feedback` queues rows; `batch_insert_voter_feedback()` runs once per bot cycle flush.

---

### `bot_decisions` — bot runner

High-level BUY/SELL/HOLD per cycle, linked to latest `tick_id` for the symbol when present.

| Column | Meaning |
|---|---|
| `decision_id` | INTEGER PRIMARY KEY AUTOINCREMENT |
| `bot_id`, `tick_id`, `mode`, `action`, `confidence`, `executed` | As names imply |
| `created_at` | **ms** — stamped by `batch_record_bot_decisions()` (older rows may have NULL) |

---

### Archive mirrors (`*_archive`)

`database._create_archive_tables()` defines **`voter_feedback_archive`**, **`market_ticks_archive`**, **`bot_decisions_archive`**, **`bot_logs_archive`** (no foreign keys) for retention / cleanup tooling. Schema mirrors the live tables’ exported columns.

---

### Other persistent tables (not WebSocket-driven but part of stored runtime data)

| Table | Role |
|---|---|
| `bots` | Bot config (`execution_mode`, `capital_source`, initial capital fields, …) |
| `bot_logs` | Structured logs (batched insert per cycle) |
| `bot_orders` | Exchange orders + raw JSON |
| `bot_capital_flows` | Deposits / withdrawals / adjustments in quote |
| `bot_risk_settings` | Per-bot risk knobs and baselines |
| `strategy_open_entries` | Per-fill entries for pyramid-style strategies |
| `app_settings` | Global keys (`execution_mode`, `global_trading_halted`, …) |
| `market_depth` | Optional order-book snapshots; **no active writer** today |

---

## Labeled data (forward returns on `voter_feedback`)

### What “labeled” means

For each `voter_feedback` row, the pipeline fills **`forward_roc_30s`** and **`forward_roc_5m`** when enough **future** `market_ticks` exist for the same `target_asset`:

- **Base price:** latest `target_price` at or before `timestamp`.
- **Forward price:** earliest `target_price` at or after `timestamp + window` (30 000 ms or 300 000 ms).
- **ROC:** `(forward − base) / base` (skipped if prices missing or base ≤ 0).

Implementation: `database._roc_for_window()` + **`label_voter_feedback_forward_roc_batch()`**. Batch labeling caches ROC calculations per `(target_asset, timestamp)` so multiple voters from the same ensemble decision reuse one price lookup set.

### Label window and freshness guard

- **`voter_feedback_label_window_bounds()`** excludes rows with `timestamp` **newer than “now minus 5 minutes”** so short horizons have time to realize (`trailing_gap_ms = 300_000`).
- Default eligible window for the live loop: last **`METAMAGI_LABEL_LOOKBACK_MINUTES`** (default **180**) minutes, intersected with that upper bound.

### Background loop vs. training

In **`backend/main.py`**, **`_meta_training_loop`**:

1. **Labels** every **`METAMAGI_LABEL_INTERVAL_SEC`** (default **30** s, not 30 minutes): runs **`label_voter_feedback_forward_roc_batch`** in small batches with tight time budgets; when bots are “due soon,” batch sizes shrink (`ACTIVE_LABEL_BATCH_SIZE`, `ACTIVE_LABEL_MAX_SECONDS`) to avoid blocking the trading thread pool.
2. **Trains** MetaMagi only if **`METAMAGI_TRAINING_ENABLED`** is truthy (`1` / `true` / …); default is **off**. When enabled, **`train_step`** runs at most every **`METAMAGI_TRAIN_INTERVAL_SEC`** (default **1800** s = 30 min) over the last **24 h** of `voter_feedback` from **`get_voter_feedback_batch(hours=24)`**.

### How MetaMagi uses labels

`backend/trading/metatrader.py`:

- Uses **`forward_roc_30s` only** for the EMA “correctness” target (rows with NULL are skipped).
- **Directional dead zone:** default `roc_threshold = 0.0005` — \(|ROC| < threshold\) counts as a **hold** outcome; above threshold, positive ROC ⇒ buy was “right,” negative ⇒ sell.
- **`forward_roc_5m`** is stored for analysis / exports but not used in the current EMA `train_step`.

### Catch-up labeling

**`metamagi_label_voter_feedback_catchup()`** (`database.py`) can drain historical unlabeled rows (optional full-table scan via `lookback_minutes=None` / env), with retries on SQLITE_BUSY — used by admin/API flows when backlogs must clear. Defaults are `METAMAGI_CATCHUP_BATCH_SIZE=500` and `METAMAGI_CATCHUP_BATCH_SLEEP_SEC=0.01`, both still tunable for slower disks or heavier live trading.

### Export / analysis

- **`scripts/metamagi_labeled_export.py`** — JSON/text bundles of labeled `voter_feedback` for offline review.
- **`scripts/forward_roc_analysis.py`** — signal vs. forward-return stats.

---

## Backtesting

| Component | Location |
|---|---|
| `BacktestEngine` | `backend/backtesting/engine.py` |
| `run_backtest.py` | `scripts/run_backtest.py` |
| `seed_ohlcv.py` | `scripts/seed_ohlcv.py` |

Lag strategies pass **`as_of_ts`** into `get_latest_lag_features()` so tick reads stay time-consistent during replay.

---

## Operational notes

- **Logical insert rate:** up to **8 symbols × 1 Hz** into the buffer; **DB flushes** default **once per 10 s** with up to **~80 rows** per flush (8 × 10), tunable via `TICK_BATCH_SEC`.
- **Stopping the server:** Cancels the collector task (`CancelledError`).
- **Implementation references:** `backend/services/data_collector.py`, `backend/services/bot_runner.py`, `backend/database.py` (`init_db`, labeling helpers), `backend/main.py` (`_meta_training_loop`).
