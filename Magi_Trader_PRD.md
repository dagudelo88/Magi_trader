**Project Name:** MagiTrader – Trading Bot Orchestration Platform  
**Version:** 1.5 (Final Local-Only Edition – Vite + SQLite)  
**Date:** March 17, 2026  
**Status:** Complete Master PRD – Ready for Module-by-Module Development  

### 1. Executive Summary
MagiTrader is a **100% local, self-hosted** web platform that lets you create, deploy, monitor, and evolve unlimited Binance trading bots with zero coding required for basic use and full Python freedom for advanced users.

Your exact vision is now embedded in the simplest possible stack:

- Every bot starts in **Simulation (Paper) mode by default** — zero money at risk.  
- Run simulations for as long as you want.  
- When a bot proves profitable, click **“Promote to Live”** — the **exact same bot** instantly switches to real Binance trading (no code changes).  
- Side-by-side Simulated vs Live P&L tracking.  
- Full data collection for future ML training.  
- Phase 2 Magi Meta-Bot (Evangelion-style): multiple bots act as independent “MAGI computers”; a central Consul evaluates every decision in real time and executes only the best one.

Everything runs **on your machine only** (localhost). No Docker, no cloud, no external services after installation. One SQLite file holds all your bots, trades, and data forever.

### 2. Core Product Goals
- Safe development: Simulation by default  
- One-click promotion to real trading when profitable  
- Central control: create strategies, start/stop bots, adjust settings  
- Performance ranking + earnings + wallet dashboard  
- Automatic data logging for ML (every tick, decision, vote)  
- Future-proof for Magi Consul ensemble voting  

### 3. User Personas
- Solo Trader (you)  
- Quant Developer  
- Portfolio Manager  
- Future AI Researcher (ML training on collected data)  

### 4. High-Level Scope
**MVP (Phase 1 – 4–6 weeks)**  
- Full Simulation → Live Promotion workflow  
- Strategy creation  
- Bot lifecycle management  
- Settings panel  
- Performance & ranking  
- Wallet monitoring  
- Data collection layer  

**Phase 2 (Magi System – +4–6 weeks)**  
- Multi-bot ensemble + Consul voting engine  

**Out of Scope for Phase 1**  
- Multi-exchange (Binance only)  
- Mobile app  
- Any cloud or Docker dependency  

### 5. System Architecture Overview
**Minimal & Local-Only Stack** (runs in two terminal windows)

**Frontend:** Vite + React 18 + Tailwind + shadcn/ui + Recharts + TanStack Query  
**Backend:** FastAPI (Python) – 4–5 files total  
**Database:** SQLite (single file: `data/magitrader.db`)  
  - **Required Tables:**
    - `bots` (id, status, mode, strategy_id)
    - `strategies` (id, code, config)
    - `trades` (id, bot_id, mode, pair, side, price, amount, timestamp)
    - `ticks_log` (for future ML, storing state at time of decision)
    - `consul_votes` (bot_id, tick_id, vote, confidence)
**Execution:** Pure Python (no Celery/Redis)  
**Binance:** CCXT (real + testnet)  

**Run commands:**
```bash
# Terminal 1 – Backend
cd backend && uvicorn main:app --reload --port 5000

# Terminal 2 – Frontend
cd frontend && npm run dev
```
→ Open http://localhost:5173

**Folder Structure**
```
magi-trader/
├── backend/                  # FastAPI
│   ├── main.py
│   ├── database.py           # SQLite connection
│   ├── models.py
│   ├── routers/              # auth, bots, strategies, performance, etc.
│   ├── services/             # simulation_engine.py + live_engine.py
│   └── requirements.txt
│
├── frontend/                 # Vite + React
│   ├── src/
│   │   ├── pages/            # Dashboard, Bots, StrategyBuilder, Settings, Performance
│   │   ├── components/       # BotCard, PromoteButton, DualPnlChart, etc.
│   │   ├── api/              # axios calls to http://localhost:5000
│   │   └── App.tsx
│   ├── vite.config.ts
│   ├── package.json
│   └── tailwind.config.js
│
├── data/
│   └── magitrader.db         # ← ALL your data (gitignored)
├── strategies/               # your .py strategy files
├── .env                      # BINANCE_API_KEY, BINANCE_SECRET, TESTNET toggle
└── README.md                 # "how to run" (exactly 2 commands)
```

### 6. Complete Module List
(Each will have its own dedicated mini-PRD later – built on the Vite + SQLite stack)

**Module 1:** Authentication & User Management (JWT + local SQLite)  
**Module 2:** Strategy Builder (No-code + full Python editor + templates)  
**Module 3:** Bot Management & Lifecycle (Promote/Demote button)  
**Module 4:** Global Settings & Configuration Panel  
**Module 5:** Wallet & Portfolio Monitor  
**Module 6:** Performance Analytics & Ranking Engine  
**Module 7:** Simulation / Paper Trading Engine (core)  
**Module 8:** Data Collection & Training Layer (SQLite + Parquet export)  
**Module 9:** Magi Meta-Bot / Consul Engine (Phase 2)  
**Module 10:** Dashboard & UI Layer  

### 7. Non-Functional Requirements (Local Focus)
- 100% localhost only – zero internet required after `npm install` and `pip install`  
- Data stays forever in `data/magitrader.db` (easy backup = copy folder)  
- Safety: Simulation default + explicit promotion + global kill switch  
- Latency: <200ms decision execution  
- Scalability: 100+ bots per user (SQLite handles it easily)  
- Security: HTTPS not needed locally; API keys encrypted in SQLite  
- Reliability: graceful recovery, full audit logs for every mode change  
- Data retention: unlimited (you control the file)  

### 8. Simulation → Live Promotion Workflow (Your Exact Requirement)
1. Create strategy → Bot created in **Simulation** mode (virtual wallet in SQLite)  
2. Run for days/weeks  
3. Platform shows **“Ready for Live”** when your criteria are met (configurable: >15% profit, Sharpe >1.5, etc.)  
4. Click **“Promote to Live”** → double confirmation  
5. Bot switches backend instantly (same Python strategy code)  
6. All historical simulation data stays attached  
7. Side-by-side Dual P&L charts (blue = sim, green = live)  
8. You can **Demote back to Simulation** anytime  

### 9. Integrations
- Binance Spot API + WebSocket (via CCXT)  
- Testnet support (toggle in .env)  
- Future: Telegram alerts, ML training hooks, Parquet/Hugging Face export  

### 10. Roadmap
**MVP Release (after all module PRDs):** Modules 1–8 + full Simulation → Live workflow  
**Magi Release (+4–6 weeks):** Module 9 (Consul voting)  
**ML Phase 3:** Auto-training on collected dataset  

### 11. Acceptance Criteria for Entire Platform
- New bot = Simulation only (enforced in code)  
- Promotion button only appears after profitable simulation (your rules)  
- Simulated and Live trades use **identical strategy code**  
- All data queryable in SQLite with `mode: "simulation" | "live"` flag  
- User can see Simulated vs Live P&L side-by-side on every bot  
- Entire app runs with exactly two terminal commands and one SQLite file  

