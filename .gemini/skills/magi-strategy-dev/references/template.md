# Strategy Template

All strategies should follow this general structure, exposing an `execute_tick` function that the MagiTrader engine will call.

```python
import ccxt
import pandas as pd

class MagiStrategy:
    def __init__(self, bot_id: str, mode: str, config: dict):
        self.bot_id = bot_id
        self.mode = mode # 'simulation' or 'live'
        self.config = config
        
    def execute_tick(self, market_data: pd.DataFrame, current_position: dict) -> dict:
        """
        Receives the latest market data and current position.
        Returns the requested action.
        """
        # Logic goes here...
        
        # Example output
        return {
            "action": "BUY", # or "SELL", "HOLD"
            "amount": 1.0,
            "metadata": {
                "confidence": 0.95
            }
        }
```

The engine handles the CCXT API calls and database logging; the strategy just decides on the action based on the input data.
