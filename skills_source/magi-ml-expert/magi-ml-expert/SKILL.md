---
name: magi-ml-expert
description: Machine Learning and Data Collection expert for MagiTrader. Use this skill when designing data logging mechanisms, Parquet exports, and pipelines for Phase 3 ML training.
---

# MagiTrader ML & Data Expert

This skill guides the implementation of Module 8 (Data Collection) and Phase 3 (ML Training) of the MagiTrader platform.

## Core Responsibility

Ensure that every market tick, bot decision, and Consul vote is immutably logged into SQLite in a format that is highly optimized for future Machine Learning extraction (e.g., exporting to Parquet or Hugging Face datasets).

## ML Data Pipeline Rules

1. **Log Everything**: The Simulation engine is also a data generation engine. Every decision a bot makes during simulation must be logged alongside the state of the market at that exact millisecond.
2. **Feature Extraction Readiness**: Store raw OHLCV and indicator values compactly in SQLite.
3. **Exporting**: Design endpoints and Python scripts that can query the SQLite `ticks_log` and `trades` tables and efficiently convert them into `.parquet` files for pandas/scikit-learn/PyTorch.

For detailed schema design for data collection, see [data-schema.md](references/data-schema.md).
