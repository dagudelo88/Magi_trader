# Magi Trader

Binance **spot** trading stack: a FastAPI backend that runs configurable bots (CCXT), a React dashboard, and SQLite persistence. Strategies are pure Python modules; ensemble “Magi” strategies combine many voter strategies into one consensus signal per bot.

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

*This README summarizes **current** implementation; for deep dives use `docs/magitrade.md` and the modules referenced above.*
