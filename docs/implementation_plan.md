# MagiTrader Implementation Plan

This document outlines the step-by-step implementation plan for the MagiTrader platform, based on the Product Requirements Document (PRD) and the System Architect guidelines.

## Phase 1: MVP (4-6 weeks)
Focus: Local-only setup, Simulation to Live workflow, and data collection.

*   **Step 1: Core Setup & Database**
    *   Initialize FastAPI backend and Vite/React frontend.
    *   Set up SQLite database (`data/magitrader.db`) with core tables: `bots`, `strategies`, `trades`, `ticks_log`, `consul_votes`.
*   **Step 2: Module 1 - Auth & User Management**
    *   Implement basic JWT authentication with local SQLite storage.
*   **Step 3: Module 2 & 3 - Strategies and Bots**
    *   Build Strategy Builder (Python code upload/editor).
    *   Implement Bot Management (CRUD operations for bots).
*   **Step 4: Module 7 - Simulation Engine**
    *   Develop the core Paper Trading Engine.
    *   Ensure new bots default to Simulation mode.
*   **Step 5: Module 3 (Extended) - Live Promotion**
    *   Implement the "Promote to Live" workflow.
    *   Integrate CCXT for live Binance execution.
*   **Step 6: Modules 5 & 6 - Wallet & Analytics**
    *   Build Wallet Monitor and Performance Analytics dashboards.
    *   Implement side-by-side Dual P&L tracking (Sim vs Live).
*   **Step 7: Module 8 - Data Collection Layer**
    *   Implement continuous logging of ticks and bot decisions for future ML training.
*   **Step 8: Module 4 & 10 - UI & Settings**
    *   Complete the Dashboard UI and Global Settings configuration panel.

## Phase 2: Magi Meta-Bot / Consul Engine (+4-6 weeks)
Focus: Evangelion-style multi-bot ensemble and consensus execution.

*   **Step 1: Vote Submission Architecture**
    *   Modify bot execution engine to output "Votes" (BUY/SELL/HOLD + Confidence) instead of direct trades.
    *   Store votes in the `consul_votes` table.
*   **Step 2: Consul Engine**
    *   Develop the central Consul that evaluates votes from multiple active bots.
    *   Implement weighting and consensus logic to determine the final trade.
*   **Step 3: Execution & Feedback**
    *   Route final Consul decisions to the live execution engine.
    *   Update UI to display bot votes and Consul decisions in real-time.

## Phase 3: Machine Learning (Future)
Focus: Auto-training on collected datasets.

*   **Step 1: Data Export**
    *   Implement Parquet/Hugging Face export from the `ticks_log` and `trades` tables.
*   **Step 2: Training Pipelines**
    *   Develop ML models to optimize strategy parameters or improve Consul voting weights.
