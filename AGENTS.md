# Agent instructions — MagiTrader

Concise context for **Cursor**, **Copilot**, **Windsurf**, **Claude Code**, and similar assistants. Humans: see **[README.md — Quick start](README.md#quick-start)** for full Windows/Linux steps.

## Bootstrap (repo root)

Run **`npm run setup`** once per clone (Node + Python on `PATH`). Equivalent scripts:

- **Windows (PowerShell):** `.\scripts\setup.ps1` — if blocked: `powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1`
- **Linux / macOS:** `./scripts/setup.sh` (`chmod +x` if needed)

Uses **`npm ci`**, **`npm ci --prefix frontend --legacy-peer-deps`**, **`pip install -r backend/requirements.txt`**.

**Note:** A root **`npm install`** (without `setup`) runs **`postinstall`**, which runs **`npm install --prefix frontend --legacy-peer-deps`** so the dashboard deps (including Vite) are not skipped.

## Secrets & env

- Template: **`backend/.env.example`** → copy to **`.env`** at repo root or **`backend/.env`** (backend file overrides root).
- **Do not** commit **`.env`** or secrets. **Do not** echo keys into logs or tests.

## Verify changes

```bash
cd backend && python -m unittest discover -s tests -p "test_*.py"
```

Use **`python3`** on Linux when **`python`** is not Python 3.

Optional manual run: **`npm run dev`** — API **`http://localhost:8000`**, UI **`http://localhost:5000`**, OpenAPI **`/docs`**.

**Windows:** **`start.bat`** at repo root ensures deps and runs **`npm run dev`** (kills stale listeners on 5000/8000, logs under **`logs/`**).

## Architecture pointers

| Area | Path |
|------|------|
| FastAPI app, routes, lifespan | `backend/main.py` |
| Bot polling / CCXT | `backend/services/bot_runner.py` |
| Market WebSocket → ticks | `backend/services/data_collector.py` |
| SQLite schema / migrations style | `backend/database.py` |
| Strategy registry | `backend/trading/strategies/registry.py` |
| React dashboard | `frontend/src/` |

## Data

- **`data/magitrader.db`** — local SQLite; absent on fresh clone until first run. WAL files **`*.db-wal`**, **`*.db-shm`** may appear next to it.

## Dependency rules

- **Python:** `backend/requirements.txt`
- **Node:** root + `frontend/` **`package.json`** and **`package-lock.json`** — frontend install needs **`--legacy-peer-deps`** (see **`npm run setup`**).
