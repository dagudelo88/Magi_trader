from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading.risk_manager import (  # type: ignore[import-not-found] # noqa: E402
    dynamic_risk_pct,
    evaluate_trade_risk,
    risk_resume_state,
    recent_volatility_pct,
)
from trading.risk_settings import (  # type: ignore[import-not-found] # noqa: E402
    DEFAULT_RISK_SETTINGS,
    normalize_risk_settings,
    template_risk_defaults,
)


def _o(
    side: str,
    *,
    amount: float,
    cost: float,
    average: float,
    created_at: int,
) -> dict:
    return {
        "side": side,
        "amount": amount,
        "filled": amount,
        "cost": cost,
        "average": average,
        "created_at": created_at,
        "symbol": "BTC/USDT",
    }


class TestRiskManager(unittest.TestCase):
    def test_dynamic_tiers(self) -> None:
        settings = normalize_risk_settings(DEFAULT_RISK_SETTINGS)
        self.assertAlmostEqual(dynamic_risk_pct(settings, 0.30), 1.0)
        self.assertAlmostEqual(dynamic_risk_pct(settings, 0.50), 2.0)
        self.assertAlmostEqual(dynamic_risk_pct(settings, 0.80), 2.8)
        self.assertAlmostEqual(dynamic_risk_pct(settings, 0.90), 3.5)

    def test_consecutive_loss_pauses(self) -> None:
        settings = normalize_risk_settings(
            {**DEFAULT_RISK_SETTINGS, "consecutive_loss_limit": 2}
        )
        orders = [
            _o("buy", amount=1, cost=100, average=100, created_at=1),
            _o("sell", amount=1, cost=90, average=90, created_at=2),
            _o("buy", amount=1, cost=100, average=100, created_at=3),
            _o("sell", amount=1, cost=95, average=95, created_at=4),
        ]
        decision = evaluate_trade_risk(
            settings=settings,
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            mark_price=100,
            consensus_score=0.50,
            ohlcv=[],
            side="buy",
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.should_pause)
        self.assertEqual(decision.consecutive_losses, 2)

    def test_manual_resume_baseline_allows_existing_loss_streak(self) -> None:
        settings = normalize_risk_settings(
            {**DEFAULT_RISK_SETTINGS, "consecutive_loss_limit": 2}
        )
        orders = [
            _o("buy", amount=1, cost=100, average=100, created_at=1),
            _o("sell", amount=1, cost=90, average=90, created_at=2),
            _o("buy", amount=1, cost=100, average=100, created_at=3),
            _o("sell", amount=1, cost=95, average=95, created_at=4),
        ]
        state = risk_resume_state(
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            now_ms=1_700_000_000_000,
        )
        decision = evaluate_trade_risk(
            settings=settings,
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            mark_price=100,
            consensus_score=0.50,
            ohlcv=[],
            side="buy",
            now_ms=1_700_000_000_000,
            risk_state=state,
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.consecutive_losses, 0)

    def test_drawdown_reduce(self) -> None:
        settings = normalize_risk_settings(
            {**DEFAULT_RISK_SETTINGS, "max_drawdown_pct": 1}
        )
        orders = [
            _o("buy", amount=1, cost=1000, average=1000, created_at=1),
            _o("sell", amount=1, cost=900, average=900, created_at=2),
        ]
        decision = evaluate_trade_risk(
            settings=settings,
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            mark_price=900,
            consensus_score=0.50,
            ohlcv=[],
            side="buy",
        )
        self.assertTrue(decision.allowed)
        self.assertAlmostEqual(decision.size_multiplier, 0.5)

    def test_drawdown_stop_blocks_and_requests_stop(self) -> None:
        settings = normalize_risk_settings(
            {
                **DEFAULT_RISK_SETTINGS,
                "max_drawdown_pct": 1,
                "drawdown_action": "stop",
            }
        )
        orders = [
            _o("buy", amount=1, cost=1000, average=1000, created_at=1),
            _o("sell", amount=1, cost=900, average=900, created_at=2),
        ]
        decision = evaluate_trade_risk(
            settings=settings,
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            mark_price=900,
            consensus_score=0.50,
            ohlcv=[],
            side="buy",
        )
        self.assertFalse(decision.allowed)
        self.assertFalse(decision.should_pause)
        self.assertTrue(decision.should_stop)

    def test_yolo_bypasses_protections_but_keeps_dynamic_sizing(self) -> None:
        now_ms = 1_700_000_100_000
        settings = normalize_risk_settings(
            {
                **DEFAULT_RISK_SETTINGS,
                "yolo_mode": True,
                "daily_loss_limit_pct": 1,
                "consecutive_loss_limit": 1,
                "max_drawdown_pct": 1,
                "drawdown_action": "pause",
                "enable_volatility_pause": True,
                "volatility_threshold": 0.01,
            }
        )
        orders = [
            _o(
                "buy",
                amount=1,
                cost=1000,
                average=1000,
                created_at=now_ms - 2_000,
            ),
            _o(
                "sell",
                amount=1,
                cost=900,
                average=900,
                created_at=now_ms - 1_000,
            ),
        ]
        volatile_ohlcv = [
            [now_ms - 60_000, 0, 0, 0, 100, 0],
            [now_ms - 50_000, 0, 0, 0, 120, 0],
            [now_ms - 40_000, 0, 0, 0, 90, 0],
            [now_ms - 30_000, 0, 0, 0, 130, 0],
        ]
        decision = evaluate_trade_risk(
            settings=settings,
            orders_oldest_first=orders,
            symbol="BTC/USDT",
            initial_capital=1000,
            mark_price=900,
            consensus_score=0.80,
            ohlcv=volatile_ohlcv,
            side="buy",
            now_ms=now_ms,
        )

        self.assertTrue(decision.allowed)
        self.assertFalse(decision.should_pause)
        self.assertAlmostEqual(decision.risk_pct or 0, 2.8)
        self.assertAlmostEqual(decision.size_multiplier, 1.0)
        self.assertLess(decision.daily_pnl or 0, 0)
        self.assertGreaterEqual(decision.consecutive_losses, 1)
        self.assertGreaterEqual(decision.drawdown_pct or 0, 1)
        self.assertIsNotNone(decision.volatility_pct)

    def test_template_lag_is_more_conservative(self) -> None:
        classic = template_risk_defaults("magi_ensemble_mid")
        lag = template_risk_defaults("magi_lag_ensemble_mid")
        self.assertLess(lag["base_risk_pct"], classic["base_risk_pct"])
        self.assertLess(lag["max_drawdown_pct"], classic["max_drawdown_pct"])

    def test_volatility_pct(self) -> None:
        closes = [100, 101, 99, 102, 98, 103]
        candles = [[0, 0, 0, 0, close, 0] for close in closes]
        self.assertIsNotNone(recent_volatility_pct(candles))


if __name__ == "__main__":
    unittest.main()
