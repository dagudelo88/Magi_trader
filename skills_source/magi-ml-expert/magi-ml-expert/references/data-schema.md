# Data Collection Schema

To effectively train ML models in Phase 3, the database must store state and actions.

## Key SQLite Tables for ML

### `market_ticks`
- `tick_id` (PK)
- `timestamp`
- `symbol`
- `open`, `high`, `low`, `close`, `volume`
- `features_json` (JSON blob containing active indicators like RSI, MACD at this tick)

### `bot_decisions`
- `decision_id` (PK)
- `bot_id` (FK)
- `tick_id` (FK)
- `mode` ("simulation" | "live")
- `action` (BUY, SELL, HOLD)
- `confidence` (Float)
- `executed` (Boolean - Did this actually result in a trade?)

### `consul_votes` (Phase 2)
- `vote_id` (PK)
- `tick_id` (FK)
- `bot_id` (FK)
- `vote_action`
- `vote_confidence`

## Export to Parquet Workflow

When asked to build the export feature, use `pandas` and `pyarrow` / `fastparquet`:

```python
import sqlite3
import pandas as pd

def export_training_data(db_path="data/magitrader.db", output_path="data/training_set.parquet"):
    conn = sqlite3.connect(db_path)
    # Join ticks and decisions to create labeled dataset
    query = """
        SELECT m.timestamp, m.symbol, m.close, m.features_json, d.action, d.confidence
        FROM market_ticks m
        JOIN bot_decisions d ON m.tick_id = d.tick_id
        WHERE d.mode = 'simulation'
    """
    df = pd.read_sql_query(query, conn)
    df.to_parquet(output_path, engine='pyarrow')
    conn.close()
```
