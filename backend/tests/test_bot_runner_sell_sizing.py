"""Sell sizing safeguards for exchange minimum notional rules."""
from __future__ import annotations

import unittest

from services.bot_runner import (  # type: ignore[import-not-found]
    _expand_sell_amount_to_avoid_dust,
)


class TestSellSizing(unittest.TestCase):
    def test_expands_when_remainder_would_be_dust(self) -> None:
        sell_amt, expanded = _expand_sell_amount_to_avoid_dust(
            sell_amt=0.00995,
            available=0.01,
            min_notional=5.0,
            last_close=80_000.0,
        )

        self.assertTrue(expanded)
        self.assertEqual(sell_amt, 0.01)

    def test_keeps_fractional_sell_when_remainder_is_sellable(self) -> None:
        sell_amt, expanded = _expand_sell_amount_to_avoid_dust(
            sell_amt=0.005,
            available=0.01,
            min_notional=5.0,
            last_close=80_000.0,
        )

        self.assertFalse(expanded)
        self.assertEqual(sell_amt, 0.005)

    def test_caps_sell_amount_to_available_position(self) -> None:
        sell_amt, expanded = _expand_sell_amount_to_avoid_dust(
            sell_amt=0.02,
            available=0.01,
            min_notional=5.0,
            last_close=80_000.0,
        )

        self.assertFalse(expanded)
        self.assertEqual(sell_amt, 0.01)


if __name__ == "__main__":
    unittest.main()
