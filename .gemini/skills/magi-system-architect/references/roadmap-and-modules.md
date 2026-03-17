# Roadmap & Modules Plan

## MVP Phase 1 (4-6 weeks)
- Module 1: Auth & User Management (local SQLite)
- Module 2: Strategy Builder
- Module 3: Bot Management & Lifecycle (Sim -> Live)
- Module 4: Global Settings
- Module 5: Wallet & Portfolio Monitor
- Module 6: Performance Analytics & Ranking
- Module 7: Simulation / Paper Trading Engine
- Module 8: Data Collection Layer

## Phase 2: Magi Meta-Bot / Consul Engine
- Multiple bots evaluate the same market tick independently.
- They act as "MAGI computers" (Melchior, Balthasar, Casper).
- Instead of executing directly, they submit a "Vote" (e.g., BUY, SELL, HOLD + Confidence Score).
- A central **Consul Engine** evaluates the votes, applies weighting or ML-based consensus, and executes the final live trade.

## Database Strategy for Architect
- The single `data/magitrader.db` must include tables for:
  - `bots` (id, status, mode, strategy_id)
  - `strategies` (id, code, config)
  - `trades` (id, bot_id, mode, pair, side, price, amount, timestamp)
  - `ticks_log` (for future ML, storing state at time of decision)
  - `consul_votes` (bot_id, tick_id, vote, confidence)
