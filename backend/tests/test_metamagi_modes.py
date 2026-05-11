"""MetaMagi mode-aware learning behavior."""
from __future__ import annotations

import unittest

from trading.metatrader import MetaTrader


class TestMetaMagiModes(unittest.TestCase):
    def test_live_uses_testnet_learning_as_prior(self) -> None:
        mt = MetaTrader()
        mt.train_step(
            [
                {
                    "execution_mode": "testnet",
                    "voter_name": "winner",
                    "voter_signal": "buy",
                    "forward_roc_30s": 0.01,
                },
                {
                    "execution_mode": "testnet",
                    "voter_name": "laggard",
                    "voter_signal": "sell",
                    "forward_roc_30s": 0.01,
                },
            ]
        )

        testnet_weights = mt.get_dynamic_weights(["winner", "laggard"], "testnet")
        live_weights = mt.get_dynamic_weights(["winner", "laggard"], "live")

        self.assertGreater(testnet_weights["winner"], testnet_weights["laggard"])
        self.assertEqual(live_weights, testnet_weights)

    def test_live_feedback_refines_live_without_rewriting_testnet(self) -> None:
        mt = MetaTrader()
        mt.train_step(
            [
                {
                    "execution_mode": "testnet",
                    "voter_name": "winner",
                    "voter_signal": "buy",
                    "forward_roc_30s": 0.01,
                },
                {
                    "execution_mode": "testnet",
                    "voter_name": "laggard",
                    "voter_signal": "sell",
                    "forward_roc_30s": 0.01,
                },
                {
                    "execution_mode": "live",
                    "voter_name": "winner",
                    "voter_signal": "sell",
                    "forward_roc_30s": 0.01,
                },
                {
                    "execution_mode": "live",
                    "voter_name": "laggard",
                    "voter_signal": "buy",
                    "forward_roc_30s": 0.01,
                },
            ]
        )

        testnet = mt.get_dynamic_weights(["winner", "laggard"], "testnet")
        live = mt.get_dynamic_weights(["winner", "laggard"], "live")

        self.assertGreater(testnet["winner"], testnet["laggard"])
        self.assertLess(live["winner"], live["laggard"])


if __name__ == "__main__":
    unittest.main()
