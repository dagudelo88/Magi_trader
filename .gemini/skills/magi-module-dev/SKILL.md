---
name: magi-module-dev
description: Expert full-stack assistant for the MagiTrader project. Use this skill when creating or modifying any of the MagiTrader modules, frontend pages, backend endpoints, or database schemas.
---

# MagiTrader Module Developer

This skill contains the constraints and architectural guidelines for developing the MagiTrader platform.

## Core Mandates

- **100% Local**: No cloud dependencies, no Docker.
- **SQLite Only**: All data must reside in a single SQLite file (`data/magitrader.db`).
- **Sim-First Execution**: Every bot starts in Simulation mode by default.

## Stack Requirements

- **Frontend**: Vite + React 18 + TailwindCSS + shadcn/ui + TanStack Query.
- **Backend**: Python (FastAPI).
- **Database**: SQLite (built-in).

## Reference Materials

For details on the architecture and folder structure, see [architecture.md](references/architecture.md).
For database design rules and the simulation-to-live execution workflow, see [execution-rules.md](references/execution-rules.md).
