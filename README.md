# Magi Trader

Binance **spot** trading stack: a FastAPI backend that runs configurable bots (CCXT), a React dashboard, and SQLite persistence. Strategies are plain Python modules; **Magi** ensemble strategies combine many voter strategies into one consensus signal per bot.

The project name and ensemble concept nod to the **Magi supercomputer** in *Neon Genesis Evangelion*—multiple analytic cores that argue toward a single decision—implemented here as voters, consensus rules, and optional MetaMagi weighting.

## Stack

| Layer | Technology |
|-------|------------|
| API | **FastAPI** (`backend/main.py`), Pydantic models, CORS, interactive OpenAPI |
| Trading | **CCXT** Binance spot; separate credentials for **testnet** vs **mainnet** |
| Persistence | **SQLite** single file `data/magitrader.db` (schema + pooling in `backend/database.py`) |
| Realtime | **WebSockets** (`/ws/bots`, `/ws/bot/{id}`, `/ws/market`) + server broadcast helper |
| Dashboard | **Vite 8**, **React 19**, **TypeScript**, **Tailwind CSS 4**, TanStack Query, lightweight-charts, Zustand |

---

## Current implementation (May 2026)

Treat this table plus the linked modules as the source of truth (long-form docs may lag).

| Area | What exists today |
|------|-------------------|
| **Process model** | On startup: `init_db()`, WebSocket manager binds to the asyncio loop, any bots marked `running` are reset to **`paused`** (must be started explicitly). Background asyncio tasks: **`bot_runner`** (polling bots), **`data_collector`** (tracked-market WebSocket → `market_ticks`), **`db_cleanup_loop`** (daily retention/archive), **`watchdog_loop`** (stale health detection), **`event_loop_lag`**. |
| **MetaMagi loop** | `_meta_training_loop` in `main.py` (periodic ROC labeling + `metatrader.train_step`) is **not** started from `lifespan` — startup logs direct operators to **manual** labeling via HTTP. Dynamic weights from `get_metatrader().get_dynamic_weights()` still merge in ensembles when accuracy state exists (e.g. after tests or if you wire the loop back). |
| **HTTP API** | Bots CRUD, fork, strategy catalog, execution mode & **promote-to-live**, capital flows, trade summary & execution history, voter signals, risk settings (+ yolo patch / reset), global trading halt, wallet balances, OHLCV & tracked markets, DB stats, **MetaMagi label catchup** (NDJSON stream), **purge sim logs**, **optimize-weights** (SSE, invokes `scripts/metamagi_labeled_export.py`). See **[/docs](#openapi--interactive-docs)** on a running server. |
| **Execution** | `backend/services/bot_runner.py`: one OHLCV fetch per bot cycle; ensembles evaluate voters in-process; **`voter_feedback`** rows for MetaMagi; orders persisted to **`bot_orders`**. |
| **Strategies** | Registry in [`backend/trading/strategies/registry.py`](backend/trading/strategies/registry.py) — see [**Strategy catalog**](#strategy-catalog) below. |
| **Risk** | Per-bot risk settings in SQLite; integration via `trading/risk_manager.py`, performance/PnL helpers in `trading/bot_performance.py`. |
| **Backtesting / tooling** | `backend/backtesting/`, `scripts/run_backtest.py`, `backtest_consensus.py`, `evaluate_bots.py`, `metamagi_labeled_export.py`, `bot_strategy_report.py`, DB helpers, etc. |
| **Tests** | `backend/tests/` — unittest-based modules (bot runner sizing, DB orders, bot performance, MetaMagi modes, risk manager, SMA cross, strategy budget). |

**Further reading:** narrative design notes in [`docs/magitrade.md`](docs/magitrade.md); data paths in [`docs/data-collection.md`](docs/data-collection.md). Where those disagree with code, **prefer the repo**.

---

## Runtime architecture (concise)

- **SQLite:** Path `data/magitrader.db` (repo root). WAL mode enabled once at init; connection pool with configurable env vars (`DB_POOL_SIZE`, … — see `database.py`).
- **Config:** `load_dotenv` loads **repo-root `.env`**, then **`backend/.env`** (backend overrides). Template variable names: [`backend/.env.example`](backend/.env.example).
- **CORS:** Defaults allow `http://localhost:5000` (Vite dev port) and common Vite preview ports; override with **`CORS_ORIGINS`** (comma-separated).
- **Frontend API origin:** Defaults to `http://localhost:8000`; override with **`VITE_API_URL`** / **`VITE_WS_URL`** in `frontend/.env` when the API is not local.

### WebSocket channels

| Path | Purpose |
|------|---------|
| `/ws/bots` | Bot list / overview updates |
| `/ws/bot/{bot_id}` | Single-bot detail stream |
| `/ws/market` | Tracked-market ticker fan-out from the collector |

---

## OpenAPI & interactive docs

With the backend running:

- **Swagger UI:** `http://localhost:8000/docs`
- **ReDoc:** `http://localhost:8000/redoc`

---

## Strategy catalog

Registered strategy **keys** (UI loads via `GET /api/strategies`):

**Standalone bots**

`sma_cross`, `supertrend`, `bb_rsi`, `macd_rsi`, `rsi_cross`, `ema_ribbon`, `dual_ema`, `stochastic`, `bb_breakout`, `parabolic_sar`, `donchian`, `tema`, `cci`, `obv_price`, `price_breakout`, `spot_grid`, `fixed_profit_rinse_repeat`

**Magi ensembles (multi-voter)**

`magi_ensemble_high` | `magi_ensemble_mid` | `magi_ensemble_low`

**Magi Lag ensembles (OHLCV + `lag_features` from `market_ticks`)**

`magi_lag_ensemble_high` | `magi_lag_ensemble_mid` | `magi_lag_ensemble_low`

**Lag-only voters (for use inside Magi Lag ensembles, not typical standalone bots)**

`btc_lead_detector`, `roc_divergence`, `lag_correlation`, `ratio_mean_reversion`

---

## Voters, ensembles, and execution path

### Voters vs bot

- **Voters** implement `evaluate(ohlcv, params)` → signal; they **do not** call the exchange.
- **Bot runner** loads one strategy module per bot row, pulls **one** OHLCV series per cycle, runs **`evaluate`** once (ensembles run many voters on the **same** candles in-process).

### Classic Magi Ensemble (`magi_ensemble_*`)

[`backend/trading/strategies/ensemble_core.py`](backend/trading/strategies/ensemble_core.py)

1. **`voters`** + optional **`voter_weights`** in `strategy_params_json`.
2. MetaMagi may supply **dynamic weights** via `get_dynamic_weights()` merged with static weights.
3. **Consensus** (`consensus_mode`, `consensus_threshold`): majority/threshold, unanimous, directional_net, etc.

### Magi Lag Ensemble (`magi_lag_ensemble_*`)

[`backend/trading/strategies/lag_ensemble_core.py`](backend/trading/strategies/lag_ensemble_core.py): same voting pattern with **`lag_features`** from lag helpers / `market_ticks`. **Ensemble strategies cannot nest** as voters.

| | Classic Magi | Magi Lag |
|--|--------------|----------|
| **Input** | OHLCV only | OHLCV + `lag_features` for lag voters |
| **Voters** | Registered OHLCV strategies | Adds lag voters + optional classics |

### From signal to trade

1. **Cooldown** — `min_trade_interval_sec` since last trade.
2. **`hold`** → no order.
3. **`buy` / `sell`** — sizing from budget / fractions / exchange limits → CCXT **market** order → **`bot_orders`** row.
4. **Feedback** — ensembles write **`voter_feedback`** (forward ROC labels filled later for MetaMagi analysis).

---

## Live vs testnet

- Each bot stores an **`execution_mode`** (`testnet` | `live`). Metrics, orders, flows, and voter feedback are separated per mode in the UI/API.
- **`POST /api/bots/{bot_id}/promote-to-live`** sets live mode with an explicit **live initial capital** allocation.
- **`POST /api/bots/{bot_id}/capital-flows`** records deposits / withdrawals / adjustments in quote terms.
- Wallet and exchange calls use testnet vs mainnet credentials from `.env` according to app trading settings.

---

## MetaMagi: labeling & weights (operator-facing)

| Mechanism | Role |
|-----------|------|
| **`POST /api/data/metamagi-label-catchup`** | Streams **NDJSON**; fills **`forward_roc_30s` / `forward_roc_5m`** on **`voter_feedback`** using **`market_ticks`** (batched, SQLite-friendly). Optional body: `lookback_days`, `max_seconds`. |
| **`POST /api/bots/{bot_id}/optimize-weights`** | **SSE** stream; runs **`scripts/metamagi_labeled_export.py`** subprocess, blends suggested edge/accuracy weights (65%/35%), persists **`voter_weights`** into **`strategy_params_json`**. |
| **Periodic `_meta_training_loop`** | Implemented in code but **not** attached in **`lifespan`** today — automatic 30‑minute-style labeling/training is off unless you wire it back into startup. |

---

## Quick start

**Requirements:** **Node.js** (LTS), **Python 3.11+** with **`pip`**, and **`.env`** (see [`backend/.env.example`](backend/.env.example)). Repo-root `.env` and `backend/.env` are both loaded.

### Install dependencies

| Layer | Declared in | Install |
|-------|-------------|---------|
| Root (`concurrently`) | [`package.json`](package.json) | `npm ci` / `npm install` at repo root |
| Frontend | [`frontend/package.json`](frontend/package.json) | `npm ci --prefix frontend` or `cd frontend && npm install` |
| Backend | [`backend/requirements.txt`](backend/requirements.txt) | `python -m pip install -r backend/requirements.txt` |

Lockfiles: **`package-lock.json`** at root and under **`frontend/`**. Python deps are mostly unpinned (some minimum versions).

**One command (recommended)**

```bash
npm run setup
```

Runs `npm ci`, `npm ci --prefix frontend --legacy-peer-deps`, then `pip` on `backend/requirements.txt`. **`--legacy-peer-deps`** works around a **`@tailwindcss/vite`** peer range vs **Vite 8** mismatch while staying aligned with committed lockfiles.

**Repo scripts (same steps)**

- **Windows:** `.\scripts\setup.ps1` — if blocked: `powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1`
- **Linux / macOS:** `./scripts/setup.sh` (`chmod +x scripts/setup.sh` if needed)

**Optional Python venv**

```bash
python -m venv .venv
# Windows: .\.venv\Scripts\Activate.ps1
# Unix:    source .venv/bin/activate
```

**Manual alternative**

```bash
npm install
cd frontend && npm install && cd ..
python -m pip install -r backend/requirements.txt
```

If the frontend install fails on peers, use `npm install --legacy-peer-deps` inside **`frontend/`**.

### Run the app

```bash
npm run dev
```

- **Backend:** `http://0.0.0.0:8000` — OpenAPI at **`/docs`**
- **Frontend:** Vite dev server on **`http://localhost:5000`** (`strictPort` in [`frontend/vite.config.ts`](frontend/vite.config.ts))

Split processes:

```bash
npm run dev:backend    # API only
npm run dev:frontend   # UI only
```

### Tests

From **`backend/`** (stdlib **unittest** discovery):

```bash
cd backend
python -m unittest discover -s tests -p "test_*.py"
```

---

## Data & SQLite

- **`data/magitrader.db`** is created on first run and is **gitignored**.
- **Backup / migrate machine:** stop the app, copy **`magitrader.db`** into **`data/`** on the new clone; if present, copy **`magitrader.db-wal`** and **`magitrader.db-shm`** together so WAL state is consistent.
- Optional tooling: `scripts/verify_db.py`, `scripts/reset_db.py`.

---

## Repository layout

| Path | Contents |
|------|----------|
| `backend/` | FastAPI app, trading strategies, services (`bot_runner`, `data_collector`, …), SQLite layer, tests |
| `frontend/` | Vite React SPA (`src/pages`, `components`, `stores`, …) |
| `data/` | Runtime SQLite (not committed) |
| `scripts/` | **`setup.ps1`**, **`setup.sh`**, backtests, MetaMagi export, DB utilities |
| `docs/` | Product / architecture notes |

---

*This README tracks **current** behavior in code; for storytelling and roadmap tone see [`docs/magitrade.md`](docs/magitrade.md).*
