# Code Review Checklist

When reviewing code for MagiTrader, explicitly check for the following:

### 1. Architectural Compliance
- [ ] **No Docker or Cloud Services**: Is the code introducing Dockerfiles, AWS SDKs, or external hosted services? If yes, **REJECT**.
- [ ] **Single Database**: Is the code using anything other than `sqlite3` (or an ORM configured strictly for SQLite) for storage? If yes, **REJECT**.
- [ ] **Pure Python Execution**: Is the code using Celery or Redis for background tasks? If yes, **REJECT**. Suggest pure Python threading or `asyncio` instead.

### 2. Strategy & Bot Execution
- [ ] **Simulation Default**: Does new bot creation strictly default to "simulation" mode?
- [ ] **Dual Compatibility**: Can the strategy run identically in both Simulation and Live engines without code changes?
- [ ] **Data Logging**: Are ticks, actions, and bot state changes being logged to the database?

### 3. Stack & Conventions
- [ ] **Frontend**: Uses React 18, Tailwind CSS, shadcn/ui components, and TanStack Query for data fetching.
- [ ] **Backend**: Uses FastAPI routers, single `main.py` entrypoint, and clear dependency injection for database sessions.
- [ ] **API Security**: API keys must be securely stored locally in SQLite (encrypted if possible) and not hardcoded or exposed to the frontend.
