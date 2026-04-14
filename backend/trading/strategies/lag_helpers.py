"""
Lag helpers — shared data-access utilities for all lag voter strategies.

Queries `market_ticks` (written at 1 Hz by data_collector) and returns
structured lag-feature dicts consumed by btc_lead_detector, roc_divergence,
lag_correlation, and ratio_mean_reversion voters.

This module is the *only* place in the lag ensemble stack that touches the DB;
the consensus engine (lag_ensemble_core) and individual voters stay pure.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default window of per-second ticks to pull from market_ticks.
DEFAULT_LOOKBACK_SEC = 60


def get_latest_lag_features(
    target_asset: str,
    lookback_sec: int = DEFAULT_LOOKBACK_SEC,
    as_of_ts: int | None = None,
) -> dict[str, Any]:
    """
    Return BTC vs. target lag features from the ``market_ticks`` table.

    Rows are ordered newest-first from the DB then reversed so that returned
    lists run oldest → newest (index 0 = oldest, index -1 = most recent).

    Returns an empty-sentinel dict if no data is available so callers can
    detect warmup conditions without raising.

    Args:
        target_asset: CCXT symbol, e.g. ``"ETH/USDT"``.
        lookback_sec: Number of 1-second ticks to fetch (default 60).
        as_of_ts:     If given (ms epoch), only ticks with
                      ``timestamp <= as_of_ts`` are considered.  Used by
                      the backtesting engine to replay historical windows
                      without touching any live data.  ``None`` (default)
                      preserves the original live behaviour.

    Returns:
        {
            "btc_closes":    list[float],   # BTC price per tick
            "target_closes": list[float],   # alt price per tick
            "btc_roc_1s":    list[float],   # BTC 1-second rate-of-change
            "btc_roc_5s":    list[float],   # BTC 5-second rate-of-change
            "target_roc_1s": list[float],
            "target_roc_5s": list[float],
            "spread_bps":    float,    # latest bid-ask spread in bps
            "features_json": dict,     # extended indicator blob (may be {})
            "latest_ratio":  float,    # btc_price / target_price (latest)
            "tick_count":    int,      # actual rows returned
        }
    """
    # Deferred import keeps module importable before backend/ is on sys.path.
    from database import get_db_connection  # type: ignore[import]

    _EMPTY: dict[str, Any] = {
        "btc_closes": [],
        "target_closes": [],
        "btc_roc_1s": [],
        "btc_roc_5s": [],
        "target_roc_1s": [],
        "target_roc_5s": [],
        "spread_bps": 0.0,
        "features_json": {},
        "latest_ratio": 1.0,
        "tick_count": 0,
    }

    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            if as_of_ts is not None:
                cur.execute(
                    """
                    SELECT btc_price, target_price,
                           btc_roc_1s, btc_roc_5s,
                           target_roc_1s, target_roc_5s,
                           spread_bps, features_json
                    FROM market_ticks
                    WHERE target_asset = ?
                      AND timestamp <= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (target_asset, as_of_ts, lookback_sec),
                )
            else:
                cur.execute(
                    """
                    SELECT btc_price, target_price,
                           btc_roc_1s, btc_roc_5s,
                           target_roc_1s, target_roc_5s,
                           spread_bps, features_json
                    FROM market_ticks
                    WHERE target_asset = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (target_asset, lookback_sec),
                )
            rows = cur.fetchall()
        finally:
            conn.close()
    except Exception:
        logger.exception("lag_helpers: DB query failed for %s", target_asset)
        return _EMPTY

    if not rows:
        return _EMPTY

    # Reverse to oldest-first order.
    rows = list(reversed(rows))

    latest = rows[-1]
    btc_latest: float = latest[0] or 0.0
    tgt_latest: float = latest[1] or 0.0
    latest_ratio: float = btc_latest / tgt_latest if tgt_latest != 0 else 1.0

    raw_features = latest[7]
    features_json: dict = {}
    if raw_features:
        try:
            features_json = json.loads(raw_features)
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "btc_closes":    [r[0] or 0.0 for r in rows],
        "target_closes": [r[1] or 0.0 for r in rows],
        "btc_roc_1s":    [r[2] or 0.0 for r in rows],
        "btc_roc_5s":    [r[3] or 0.0 for r in rows],
        "target_roc_1s": [r[4] or 0.0 for r in rows],
        "target_roc_5s": [r[5] or 0.0 for r in rows],
        "spread_bps":    latest[6] or 0.0,
        "features_json": features_json,
        "latest_ratio":  round(latest_ratio, 8),
        "tick_count":    len(rows),
    }
