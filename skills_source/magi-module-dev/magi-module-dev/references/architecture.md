# Architecture & Folder Structure

## Project Structure

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
│   │   ├── api/              # axios calls to http://localhost:8000
│   │   └── App.tsx
│   ├── vite.config.ts
│   ├── package.json
│   └── tailwind.config.js
│
├── data/
│   └── magitrader.db         # ← ALL your data
├── strategies/               # your .py strategy files
└── .env                      # BINANCE_API_KEY, BINANCE_SECRET, TESTNET toggle
```

- **Execution Commands**
  - Backend: `cd backend && uvicorn main:app --reload --port 8000`
  - Frontend: `cd frontend && npm run dev`
