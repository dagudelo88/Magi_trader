# Magi Trader

Binance **spot** trading stack: a FastAPI backend that runs configurable bots (CCXT), a React dashboard, and SQLite persistence. Strategies are pure Python modules; ensemble “Magi” strategies combine many voter strategies into one consensus signal per bot.

The project name and ensemble concept are inspired by the **Magi supercomputer** in *Neon Genesis Evangelion*—multiple analytic cores that debate and reach a single decision—adapted here as configurable voters and consensus rules.

## Current status (April 2026)

| Area | Status |
|------|--------|
| **API & runtime** | FastAPI app (`backend/main.py`): bots CRUD, execution modes, global halt, order sync, performance helpers, MetaMagi background loop. |
| **Execution** | `backend/services/bot_runner.py` runs polling bots, logs ensemble voter breakdowns, persists **`voter_feedback`** for MetaMagi. |
| **Strategies** | Registry-driven (`backend/trading/strategies/registry.py`): SMA, Supertrend, BB/RSI, MACD+RSI, EMA ribbon, dual EMA, stochastic, breakouts, grid, etc. |
| **Magi Ensemble** | **`magi_ensemble_{high,mid,low}`** — configurable voters, consensus + optional MetaMagi dynamic weights (`ensemble_core.py`). |
| **Magi Lag Ensemble** | **`magi_lag_ensemble_{high,mid,low}`** — lead/lag–aware voters (`lag_ensemble_core.py`, `btc_lead_detector`, `roc_divergence`, `lag_correlation`, `ratio_mean_reversion`). |
| **MetaMagi** | `backend/trading/metatrader.py` + startup loop in `main.py`: labels forward ROC from `market_ticks`, updates voter weight EMAs on a fixed interval (no extra exchange calls). |
| **Data** | SQLite schema in `backend/database.py` includes bots, orders, **`voter_feedback`**, **`market_ticks`**, and related indexes. |
| **Backtesting / scripts** | `backend/backtesting/`, `scripts/run_backtest.py`, `backtest_consensus.py`, `evaluate_bots.py`, DB helpers, etc. |
| **Frontend** | Vite + React + TypeScript (`frontend/`), TanStack Query, charts — bot templates and UI aligned with strategy catalog. |
| **Tests** | `backend/tests/` — bot orders DB, strategy budget, SMA cross, bot performance (expand as features grow). |

**Docs:** design and ensemble rationale live in [`docs/magitrade.md`](docs/magitrade.md) (some “next steps” there are **already implemented** in this repo — treat the codebase as source of truth).

## Voters, ensembles, and how trades run

### Voters vs bot

- **Voters** are individual strategies registered in [`backend/trading/strategies/registry.py`](backend/trading/strategies/registry.py) (e.g. SMA cross, MACD+RSI). Each returns `buy`, `sell`, or `hold` from OHLCV (and sometimes extra features). They **do not** call the exchange.
- The **bot** is [`backend/services/bot_runner.py`](backend/services/bot_runner.py): one DB row per bot. Each cycle it fetches **one** OHLCV series, calls **`strategy.evaluate(ohlcv, params)`** once, and **only that final signal** can trigger an order. Ensembles run many voters **in-process** on the same candles (no extra REST calls per voter).

### Classic Magi Ensemble (`magi_ensemble_high` / `mid` / `low`)

Implemented in [`backend/trading/strategies/ensemble_core.py`](backend/trading/strategies/ensemble_core.py):

1. Configure a **`voters`** list and optional **`voter_weights`** in the bot’s `strategy_params_json`.
2. Each voter runs `evaluate` on the **same** OHLCV; votes are weighted.
3. [**MetaMagi**](backend/trading/metatrader.py) may override weights via `get_dynamic_weights` from learned feedback.
4. **Consensus** (`consensus_mode`, `consensus_threshold`) turns weighted votes into one signal: e.g. **majority** / **threshold**, **unanimous**, or **directional_net** (net buy–sell pressure vs total weight).

### Magi Lag Ensemble (`magi_lag_ensemble_*`)

Implemented in [`backend/trading/strategies/lag_ensemble_core.py`](backend/trading/strategies/lag_ensemble_core.py): same voting idea, but the runner also passes **`lag_features`** (from `lag_helpers` / `market_ticks`) into each voter. Lag-specific voters (`btc_lead_detector`, `roc_divergence`, `lag_correlation`, `ratio_mean_reversion`) use that; classic OHLCV voters ignore it. **Ensemble strategies cannot nest** as voters (recursion guards).

| | Classic Magi | Magi Lag |
|--|--------------|----------|
| **Input** | OHLCV only | OHLCV + `lag_features` for lag voters |
| **Voters** | Allowlisted OHLCV strategies | Same + lag voters + optional classics |

### From signal to trade

1. **Cooldown** — `min_trade_interval_sec` since the bot’s last trade.
2. **Signal** — `evaluate` returns `hold` → log and exit; **no order**.
3. **Buy/sell** — Load balances and market limits; apply **`initial_budget_quote`**, **`quote_fraction`** / **`base_fraction`**, exchange min notional; then **`create_order`** (market) via CCXT and record the order.
4. **Feedback** — For ensembles, [`_log_voter_feedback`](backend/services/bot_runner.py) writes per-voter rows to **`voter_feedback`** for MetaMagi (labels are filled later); this **does not** decide execution.

## Quick start

**Requirements:** Python 3 with dependencies from `backend/requirements.txt`, Node.js for the frontend, and a configured `.env` (see backend for variables used by exchange and DB paths).

From the repo root:

```bash
npm install
cd frontend && npm install && cd ..
npm run dev
```

- Backend: `http://0.0.0.0:8000` (see `package.json` scripts).
- Frontend: Vite dev server (port shown in the terminal).

Alternatively run only the API: `npm run dev:backend` or only the UI: `npm run dev:frontend`.

## Repository layout

- `backend/` — FastAPI, trading logic, services, SQLite, tests.
- `frontend/` — React app.
- `docs/` — architecture and product notes.
- `scripts/` — maintenance, backtests, analysis utilities.

---

*This README summarizes **current** implementation; for more narrative detail see `docs/magitrade.md` and the modules linked above.*
