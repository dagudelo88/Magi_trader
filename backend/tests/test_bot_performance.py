"""FIFO realized PnL and win-rate from stored orders."""
from __future__ import annotations

import unittest

from trading.bot_performance import compute_strategy_performance


class TestBotPerformance(unittest.TestCase):
    def test_buy_sell_profit(self) -> None:
        orders = [
            _o("buy", amount=1.0, cost=100.0, average=100.0),
            _o("sell", amount=1.0, cost=110.0, average=110.0),
        ]
        p = compute_strategy_performance(orders, "BTC/USDT", mark_price=None)
        self.assertAlmostEqual(p["realized_pnl_quote"], 10.0)
        self.assertEqual(p["closed_trades"], 1)
        self.assertEqual(p["winning_trades"], 1)
        self.assertEqual(p["losing_trades"], 0)
        self.assertAlmostEqual(p["win_rate_pct"], 100.0)

    def test_buy_sell_loss(self) -> None:
        orders = [
            _o("buy", amount=2.0, cost=200.0, average=100.0),
            _o("sell", amount=2.0, cost=180.0, average=90.0),
        ]
        p = compute_strategy_performance(orders, "BTC/USDT", mark_price=None)
        self.assertAlmostEqual(p["realized_pnl_quote"], -20.0)
        self.assertEqual(p["losing_trades"], 1)
        self.assertEqual(p["winning_trades"], 0)

    def test_fifo_two_buys_one_sell(self) -> None:
        orders = [
            _o("buy", amount=1.0, cost=100.0, average=100.0),
            _o("buy", amount=1.0, cost=150.0, average=150.0),
            _o("sell", amount=1.0, cost=120.0, average=120.0),
        ]
        p = compute_strategy_performance(orders, "BTC/USDT", mark_price=None)
        self.assertAlmostEqual(p["realized_pnl_quote"], 20.0)
        self.assertAlmostEqual(p["open_base_position"], 1.0)
        self.assertAlmostEqual(p["open_cost_basis_quote"], 150.0)

    def test_unrealized_with_mark(self) -> None:
        orders = [_o("buy", amount=1.0, cost=100.0, average=100.0)]
        p = compute_strategy_performance(orders, "BTC/USDT", mark_price=110.0)
        self.assertAlmostEqual(p["unrealized_pnl_quote"], 10.0)
        self.assertEqual(p["closed_trades"], 0)

    def test_win_rate_two_exits(self) -> None:
        orders = [
            _o("buy", amount=1.0, cost=100.0, average=100.0),
            _o("sell", amount=1.0, cost=110.0, average=110.0),
            _o("buy", amount=1.0, cost=100.0, average=100.0),
            _o("sell", amount=1.0, cost=95.0, average=95.0),
        ]
        p = compute_strategy_performance(orders, "BTC/USDT", mark_price=None)
        self.assertEqual(p["closed_trades"], 2)
        self.assertEqual(p["winning_trades"], 1)
        self.assertEqual(p["losing_trades"], 1)
        self.assertAlmostEqual(p["win_rate_pct"], 50.0)


def _o(side: str, *, amount: float, cost: float, average: float) -> dict:
    return {
        "side": side,
        "amount": amount,
        "cost": cost,
        "average": average,
        "filled": amount,
        "created_at": 0,
        "symbol": "BTC/USDT",
    }


if __name__ == "__main__":
    unittest.main()
