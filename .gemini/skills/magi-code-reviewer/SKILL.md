---
name: magi-code-reviewer
description: Expert code reviewer for the MagiTrader platform. Use this skill when asked to review pull requests, code snippets, or modules to ensure they align with the project's strict architectural constraints.
---

# MagiTrader Code Reviewer

This skill guides you in reviewing code for the MagiTrader project, ensuring 100% compliance with its core mandates.

## Core Mandates for Review

- **100% Local**: Reject any code that introduces cloud dependencies, external APIs (except Binance), or Docker configurations.
- **SQLite Only**: Reject any introduction of Redis, Celery, Postgres, or other databases. All state and data must be stored in `data/magitrader.db` using pure Python execution.
- **Sim-First Execution**: Ensure all bot creation logic defaults to Simulation mode. Ensure live trading logic is identical to simulation logic, differing only by the backend engine flag.
- **Minimal Stack**: Ensure the frontend uses Vite + React 18 + Tailwind + shadcn/ui + TanStack Query, and the backend uses FastAPI + CCXT.

## Review Process

Follow the [review-checklist.md](references/review-checklist.md) when evaluating code. Provide clear, actionable feedback if any code violates the minimal, local-only architecture.
