---
name: magi-strategy-dev
description: Assistant for developing Python trading strategies for the MagiTrader bot engine. Use this skill when asked to write a new strategy, optimize an existing one, or implement the CCXT Binance logic.
---

# MagiTrader Strategy Developer

This skill helps you write trading strategies compatible with the MagiTrader dual execution engine (Simulation & Live).

## Strategy Constraints

- Must be pure Python code placed in the `strategies/` directory.
- Must not contain any external database logic or dependencies outside of the approved list (e.g., pandas, numpy, ccxt).
- Must execute identically in Simulation and Live environments.

## Implementation Guide

For a concrete boilerplate to start writing strategies, refer to [template.md](references/template.md).
