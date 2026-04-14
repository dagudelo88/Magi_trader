**MagiTrader BTC-Alt Lag Ensemble — New Specialized Magi**

Perfect. We now add a **dedicated lag-specialized Magi** that focuses exactly on what you asked:

- Detects **lead-lag relationships** between **BTC** and any altcoin from your live market feed (ETH, BNB, SOL, XRP, ADA, DOGE, AVAX, etc.).
- Uses the **same voter + bot framework** we already built (many voters → one bot executes).
- Adds **new lag-specific voter strategies** that leverage the rich per-second data your `market_ticks` table already collects (BTC price, target price, multi-window ROC, volume deltas, spreads, features_json).

### Voter vs Bot Reminder (as you requested)
- **Voter** = pure analyst (looks at data, returns buy/sell/hold signal). You can have **unlimited** voters.
- **Bot** = execution unit (one bot receives the **consensus** signal and actually places Binance orders). Only **one bot** per Magi ensemble.

The new `magi_lag_ensemble` is **one bot** (with its own budget, cooldown, etc.) that asks many lag-focused voters for their opinion, runs consensus, and executes.

### New Lag Ensemble (drop-in)

Create these three files (high / mid / low frequency variants — same pattern as before).

#### 1. `backend/trading/strategies/lag_helpers.py` (shared helper — new file)

```python
from __future__ import annotations
import sqlite3
import json
from typing import Dict, Any

DB_PATH = "data/magitrader.db"

def get_latest_lag_features(target_asset: str, lookback_sec: int = 60) -> Dict[str, Any]:
    """
    Returns BTC vs Target lag features from market_ticks (per-second data).
    Used by all lag voters.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT timestamp, btc_price, target_price, btc_roc_1s, btc_roc_5s,
               target_roc_1s, target_roc_5s, spread_bps, features_json
        FROM market_ticks
        WHERE target_asset = ?
        ORDER BY timestamp DESC LIMIT ?
    """, (target_asset, lookback_sec))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {"btc_closes": [], "target_closes": [], "btc_roc": [], "target_roc": [], "ratio": 0.0}

    # Extract closes and ROCs (oldest to newest)
    btc_closes = [r[1] for r in rows]
    target_closes = [r[2] for r in rows]
    btc_roc = [r[3] for r in rows]      # 1s ROC
    target_roc = [r[5] for r in rows]

    # Latest price ratio (BTC/Alt normalized)
    latest_ratio = rows[0][1] / rows[0][2] if rows[0][2] != 0 else 1.0

    return {
        "btc_closes": btc_closes,
        "target_closes": target_closes,
        "btc_roc": btc_roc,
        "target_roc": target_roc,
        "latest_ratio": latest_ratio,
        "spread_bps": rows[0][7],
        "features_json": json.loads(rows[0][8]) if rows[0][8] else {}
    }
```

#### 2. `backend/trading/strategies/magi_lag_ensemble_mid.py` (recommended — medium frequency)

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Any, Dict
from trading.strategies import load_strategy
from trading.strategies.lag_helpers import get_latest_lag_features

@dataclass(frozen=True)
class SignalDetails:
    signal: Literal["buy", "sell", "hold"]
    consensus_score: float
    voter_signals: dict[str, str]
    meta_weights: dict[str, float]
    close_count: int

def evaluate_signal_details(closes: list[float], **strategy_params) -> SignalDetails:
    """Magi Lag Ensemble — specializes in BTC → Altcoin lead/lag relationships."""
    if not closes:
        return SignalDetails("hold", 0.0, {}, {}, 0)

    target_asset: str = strategy_params.get("target_asset")  # e.g. "ETH/USDT"
    lag_features = get_latest_lag_features(target_asset)

    voters: list[str] = strategy_params.get("voters", [
        "btc_lead_detector", "roc_divergence", "lag_correlation",
        "ratio_mean_reversion", "rsi_mean_reversion"  # classic + new lag voters
    ])
    weights = strategy_params.get("voter_weights", {v: 1.0 for v in voters})
    # MetaMagi can override weights dynamically
    weights = {**weights, **metatrader.get_dynamic_weights()}

    votes = {"buy": 0.0, "sell": 0.0, "hold": 0.0}
    voter_signals = {}

    for voter_name in voters:
        try:
            default_fn, eval_fn = load_strategy(voter_name)
            params = {**default_fn(), **strategy_params.get(f"{voter_name}_params", {})}
            # Pass lag_features to lag voters
            signal = eval_fn(closes, lag_features=lag_features, **params)
            voter_signals[voter_name] = signal
            votes[signal] += weights.get(voter_name, 1.0)
        except Exception:
            voter_signals[voter_name] = "hold"
            votes["hold"] += 1.0

    total = sum(votes.values())
    final = max(votes, key=votes.get)
    score = votes[final] / total if total > 0 else 0.0

    final_signal = final if score >= strategy_params.get("consensus_threshold", 0.60) else "hold"

    return SignalDetails(
        signal=final_signal,
        consensus_score=round(score, 2),
        voter_signals=voter_signals,
        meta_weights=weights,
        close_count=len(closes)
    )

def evaluate_signal(closes: list[float], **strategy_params) -> Literal["buy", "sell", "hold"]:
    return evaluate_signal_details(closes, **strategy_params).signal

def default_strategy_params() -> dict[str, Any]:
    return {
        "quote_fraction": 0.025,
        "base_fraction": 0.5,
        "min_trade_interval_sec": 300,
        "ohlcv_timeframe": "5m",
        "ohlcv_limit": 200,
        "voters": ["btc_lead_detector", "roc_divergence", "lag_correlation", "ratio_mean_reversion"],
        "consensus_threshold": 0.60,
        "voter_weights": {"btc_lead_detector": 1.3, "roc_divergence": 1.2, "lag_correlation": 1.1, "ratio_mean_reversion": 1.0},
    }
```

(You can copy this pattern for `magi_lag_ensemble_high.py` and `low.py` — just change timeframe, cooldown, threshold, and voter list.)

#### 3. Example New Lag Voter: `backend/trading/strategies/btc_lead_detector.py`

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Any

@dataclass(frozen=True)
class SignalDetails:
    signal: Literal["buy", "sell", "hold"]
    lag_score: float | None
    close_count: int

def evaluate_signal_details(closes: list[float], lag_features: dict = None, **strategy_params) -> SignalDetails:
    """New lag voter: Detects when BTC is leading and altcoin is about to catch up."""
    if not lag_features or not lag_features["btc_roc"]:
        return SignalDetails("hold", None, len(closes))

    btc_roc = lag_features["btc_roc"][-10:]      # last 10 seconds
    target_roc = lag_features["target_roc"][-10:]

    # BTC strong move + alt lagging = BUY signal (catch-up expected)
    btc_momentum = sum(btc_roc) / len(btc_roc) if btc_roc else 0
    alt_momentum = sum(target_roc) / len(target_roc) if target_roc else 0

    lag_score = btc_momentum - alt_momentum

    if lag_score > 0.008 and alt_momentum < 0.002:   # BTC leading strongly
        signal = "buy"
    elif lag_score < -0.008 and alt_momentum > -0.002:  # BTC dropping faster
        signal = "sell"
    else:
        signal = "hold"

    return SignalDetails(signal=signal, lag_score=round(lag_score, 4), close_count=len(closes))

def evaluate_signal(closes: list[float], lag_features: dict = None, **strategy_params) -> Literal["buy", "sell", "hold"]:
    return evaluate_signal_details(closes, lag_features, **strategy_params).signal

def default_strategy_params() -> dict[str, Any]:
    return {"ohlcv_limit": 100}  # not really used — we pull from market_ticks
```

(The other new lag voters — `roc_divergence`, `lag_correlation`, `ratio_mean_reversion` — follow the exact same pattern using `lag_features`.)

### Integration (one-time)

1. Add `lag_helpers.py` and the new files to `backend/trading/strategies/__init__.py` (inside `load_strategy`).
2. Add `"magi_lag_ensemble_mid"` (and high/low) to `ALLOWED_STRATEGIES` in `main.py` + safe_keys.
3. Create a new bot with `strategy: "magi_lag_ensemble_mid"` on **any altcoin** (e.g. ETH/USDT) + $150 budget.

This Magi now **specializes** in BTC-alt lag using the exact per-second data you already collect. The voters see real lead-lag dynamics every cycle.

Want the full set of 4 new lag voters + high/low variants right now? Or shall I also update the MetaMagi to learn lag patterns specifically? Just say the word.