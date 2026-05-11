"""Sell sizing safeguards for exchange minimum notional rules."""
from __future__ import annotations

import unittest

from services.bot_runner import (  # type: ignore[import-not-found]
    _expand_sell_amount_to_avoid_dust,
    _full_position_is_unsellable_dust,
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

    def test_treats_full_position_below_min_amount_as_dust(self) -> None:
        is_dust, full_notional = _full_position_is_unsellable_dust(
            available=0.000009,
            min_amount=0.00001,
            min_notional=5.0,
            last_close=81_666.18,
        )

        self.assertTrue(is_dust)
        self.assertAlmostEqual(full_notional, 0.7350, places=4)

    def test_allows_full_position_that_clears_exchange_filters(self) -> None:
        is_dust, full_notional = _full_position_is_unsellable_dust(
            available=0.0001,
            min_amount=0.00001,
            min_notional=5.0,
            last_close=81_666.18,
        )

        self.assertFalse(is_dust)
        self.assertAlmostEqual(full_notional, 8.1666, places=4)


if __name__ == "__main__":
    unittest.main()
