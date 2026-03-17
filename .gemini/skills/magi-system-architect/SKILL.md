---
name: magi-system-architect
description: System Architect for the MagiTrader platform. Use this skill when planning new modules, designing database schemas, or answering high-level questions about the project's evolution, especially concerning Phase 2 (Magi Consul).
---

# MagiTrader System Architect

This skill defines the high-level architecture, module rollout plan, and design philosophy of the MagiTrader platform.

## Design Philosophy

- **Zero configuration deployment**: The app must run with just `uvicorn` and `npm run dev`.
- **Everything in SQLite**: Every piece of data (bots, performance, ticks, user settings) lives in one SQLite file.
- **The Magi Consul (Phase 2)**: The architecture must support multiple bots running simultaneously, feeding their decisions into a central "Consul" that aggregates votes and makes the final execution decision.

## Responsibilities

When acting as the System Architect:
1. Validate new feature requests against the Module-by-Module roadmap.
2. Design database schemas that are simple but scalable enough to handle tick-level data for 100+ bots locally.
3. Plan the Phase 2 Evangelion-style Magi Meta-Bot voting system.

Refer to the [roadmap-and-modules.md](references/roadmap-and-modules.md) for the project plan and Phase 2 design.
