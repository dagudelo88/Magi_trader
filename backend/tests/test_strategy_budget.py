"""Strategy params: initial budget parsing and merge."""
from __future__ import annotations

import json
import unittest

from trading.strategy_budget import (
    initial_budget_from_params_dict,
    initial_budget_from_strategy_params_json,
    merge_strategy_params_json,
    parse_initial_budget_api_value,
)


class TestStrategyBudget(unittest.TestCase):
    def test_keys(self) -> None:
        self.assertEqual(initial_budget_from_params_dict({"initial_budget_quote": 500}), 500.0)
        self.assertEqual(initial_budget_from_params_dict({"trading_budget_quote": "200"}), 200.0)
        self.assertEqual(initial_budget_from_params_dict({"budget_usdt": 100}), 100.0)

    def test_invalid_ignored(self) -> None:
        self.assertIsNone(initial_budget_from_params_dict({"initial_budget_quote": 0}))
        self.assertIsNone(initial_budget_from_params_dict({"initial_budget_quote": -10}))
        self.assertIsNone(initial_budget_from_params_dict({"initial_budget_quote": "x"}))

    def test_json_string(self) -> None:
        raw = json.dumps({"fast_period": 5, "initial_budget_quote": 250})
        self.assertEqual(initial_budget_from_strategy_params_json(raw), 250.0)

    def test_merge(self) -> None:
        m = merge_strategy_params_json('{"fast_period": 5}', {"initial_budget_quote": 100})
        self.assertEqual(m["fast_period"], 5)
        self.assertEqual(m["initial_budget_quote"], 100)

    def test_parse_api(self) -> None:
        self.assertIsNone(parse_initial_budget_api_value(None))
        self.assertIsNone(parse_initial_budget_api_value(""))
        self.assertIsNone(parse_initial_budget_api_value(0))
        self.assertEqual(parse_initial_budget_api_value(250), 250.0)
        self.assertEqual(parse_initial_budget_api_value("100.5"), 100.5)
        with self.assertRaises(ValueError):
            parse_initial_budget_api_value("x")
        with self.assertRaises(ValueError):
            parse_initial_budget_api_value(-1)


if __name__ == "__main__":
    unittest.main()
