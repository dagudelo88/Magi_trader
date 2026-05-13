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

**Requirements:** **Node.js** (LTS) + **npm**, **Python 3.11+** with **`pip`**, and a **`.env`** at the repo root (copy from [`backend/.env.example`](backend/.env.example)). Both **repo-root `.env`** and **`backend/.env`** are loaded if present (backend overrides).

What the installers do: **`npm ci`** at the repo root, **`npm ci --prefix frontend --legacy-peer-deps`**, then **`pip install -r backend/requirements.txt`**. The **`--legacy-peer-deps`** flag matches the committed lockfiles while avoiding a **`@tailwindcss/vite`** vs **Vite 8** peer conflict.

| Layer | Declared in |
|-------|-------------|
| Root (`concurrently`) | [`package.json`](package.json) |
| Frontend | [`frontend/package.json`](frontend/package.json) (+ root / frontend **`package-lock.json`**) |
| Backend | [`backend/requirements.txt`](backend/requirements.txt) |

### Coding agents (Cursor, Copilot, Windsurf, …)

If you use an **AI coding assistant** to work in this repo, point it at this section so setup stays reproducible.

| Goal | What to tell the agent |
|------|-------------------------|
| **Install everything** | Run from the **repo root**: **`npm run setup`** (cross‑platform). Same behavior as [`scripts/setup.ps1`](scripts/setup.ps1) (Windows PowerShell) or [`scripts/setup.sh`](scripts/setup.sh) (Linux/macOS bash). Prefer **non‑interactive** commands; no prompts in those scripts. |
| **Environment** | Copy [`backend/.env.example`](backend/.env.example) → **`.env`** at repo root (or `backend/.env`). **Never commit** `.env` or paste live API secrets into chat. |
| **Sanity check** | **`cd backend && python -m unittest discover -s tests -p "test_*.py"`** — use **`python3`** on Linux if `python` is not v3. Optional: **`npm run dev`** then open **`http://localhost:5000`** (UI) and **`http://localhost:8000/docs`** (API). |
| **Where things live** | HTTP API: [`backend/main.py`](backend/main.py). Strategies: [`backend/trading/strategies/`](backend/trading/strategies/). SQLite schema / pooling: [`backend/database.py`](backend/database.py). Dashboard: [`frontend/src/`](frontend/src/). |
| **Local data** | **`data/magitrader.db`** is runtime state — **gitignored**; do not treat a missing DB as a bug on fresh clone. |

Repo-root **[`AGENTS.md`](AGENTS.md)** mirrors this for tools that load agent instructions automatically (e.g. Cursor).

---

### Windows (PowerShell)

1. **Install prerequisites:** [Node.js LTS](https://nodejs.org/) (includes npm) and [Python 3](https://www.python.org/) — during Python setup, enable **Add python.exe to PATH**. The Windows **`py`** launcher is optional; [`scripts/setup.ps1`](scripts/setup.ps1) tries `python` first, then `py -3`.

2. **Clone** this repo and **open a terminal in the repo root** (the folder that contains `package.json`).

3. **Environment:** copy [`backend/.env.example`](backend/.env.example) to **`.env`** at the repo root (or under `backend/`) and fill in keys.

4. **Install dependencies** — choose one:

   **A — Repo script (recommended)** — same steps as `npm run setup`, with clearer errors if Node/Python are missing:

   ```powershell
   .\scripts\setup.ps1
   ```

   If execution policy blocks it:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
   ```

   **B — npm script**

   ```powershell
   npm run setup
   ```

5. **Run the app**

   ```powershell
   npm run dev
   ```

**Windows notes**

- If **`npm ci`** fails with **EPERM** / **unlink** on a file under `node_modules`, stop other processes using the repo (running dev server, IDE indexing, antivirus scan) and run the install again.
- Prefer **PowerShell** or **Windows Terminal** for `setup.ps1`; **CMD** does not run `.ps1` files natively.

---

### Linux

These steps also work on **macOS** in **Terminal** (use **`./scripts/setup.sh`** the same way).

1. **Install prerequisites** (names vary by distro):

   - **Node.js LTS** — from your distro packages, [NodeSource](https://github.com/nodesource/distributions), [nvm](https://github.com/nvm-sh/nvm), or [fnm](https://github.com/Schniz/fnm).
   - **Python 3** + **`pip`** — e.g. Debian/Ubuntu: `sudo apt update && sudo apt install -y python3 python3-pip python3-venv`.

2. **Clone** the repo and **`cd`** into the repo root.

3. **Environment:** copy [`backend/.env.example`](backend/.env.example) to **`.env`** at the repo root (or `backend/.env`) and edit values.

4. **Install dependencies** — choose one:

   **A — Repo script (recommended)**

   ```bash
   chmod +x scripts/setup.sh    # only if the file is not executable yet
   ./scripts/setup.sh
   ```

   [`scripts/setup.sh`](scripts/setup.sh) uses **`python3`** if available, otherwise **`python`**.

   **B — npm script**

   ```bash
   npm run setup
   ```

5. **Run the app**

   ```bash
   npm run dev
   ```

**Linux notes**

- If **`npm ci`** reports missing **`package-lock.json`**, run **`git checkout`** / **`git pull`** so lockfiles are present; do not delete them for installs.
- Rare native-addon build failures on minimal images may require **`build-essential`** (Debian/Ubuntu) or your distro’s compiler toolchain.

---

### Optional: Python virtualenv (Windows or Linux)

```bash
python -m venv .venv
```

Activate, then run **`npm run setup`** or the OS script so **`pip`** installs into the venv:

| OS | Activate |
|----|----------|
| Windows (PowerShell) | `.\.venv\Scripts\Activate.ps1` |
| Linux / macOS | `source .venv/bin/activate` |

---

### Manual install (no scripts)

If you prefer not to use [`scripts/setup.ps1`](scripts/setup.ps1) / [`scripts/setup.sh`](scripts/setup.sh) or **`npm run setup`**:

```bash
npm install
cd frontend && npm install --legacy-peer-deps && cd ..
python -m pip install -r backend/requirements.txt
```

On Linux, use **`python3 -m pip`** if **`python`** is not Python 3.

---

### Run URLs & split processes

- **Backend:** `http://0.0.0.0:8000` — OpenAPI at **`/docs`**
- **Frontend:** **`http://localhost:5000`** (`strictPort` in [`frontend/vite.config.ts`](frontend/vite.config.ts))

```bash
npm run dev             # API + UI together
npm run dev:backend     # API only
npm run dev:frontend    # UI only
```

### Tests

From **`backend/`** (stdlib **unittest** discovery):

```bash
cd backend
python -m unittest discover -s tests -p "test_*.py"
```

On Linux, if `python` is not Python 3, use **`python3`** instead of **`python`**.

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
| `AGENTS.md` | Short instructions for **AI coding agents** (Cursor, etc.) — bootstrap, secrets, test command |
| `docs/` | Product / architecture notes |

---

*This README tracks **current** behavior in code; for storytelling and roadmap tone see [`docs/magitrade.md`](docs/magitrade.md).*
