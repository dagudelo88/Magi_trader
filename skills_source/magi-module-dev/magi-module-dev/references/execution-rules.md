# Execution & Database Rules

## Simulation → Live Promotion Workflow
1. New bot = Simulation only (enforced in code).
2. The platform shows “Ready for Live” when criteria are met.
3. User clicks “Promote to Live” → double confirmation.
4. Bot switches backend instantly (same Python strategy code).
5. All historical simulation data stays attached.
6. Side-by-side Dual P&L charts (blue = sim, green = live).
7. Can Demote back to Simulation anytime.

## Database Rules
- Use a single SQLite database file: `data/magitrader.db`.
- No Celery or Redis for execution; pure Python threads/asyncio only.
- Include a `mode` flag (`"simulation" | "live"`) for data queries.
