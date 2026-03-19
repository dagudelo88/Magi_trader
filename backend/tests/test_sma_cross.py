import unittest

from trading.strategies.sma_cross import evaluate_signal, evaluate_signal_details


class TestSmaCross(unittest.TestCase):
    def test_hold_when_not_enough_data(self):
        closes = [1.0, 2.0, 3.0]
        self.assertEqual(evaluate_signal(closes, 2, 3), "hold")

    def test_details_matches_evaluate_signal(self):
        closes = [1.0, 2.0, 3.0, 4.0, 5.0, 4.0, 3.0]
        self.assertEqual(
            evaluate_signal_details(closes, 2, 3).signal,
            evaluate_signal(closes, 2, 3),
        )

    def test_buy_on_fast_cross_up(self):
        # Slow SMA stays below fast after cross: craft closes so at last bar fast crosses above slow
        closes = [
            10.0,
            10.0,
            10.0,
            10.0,
            10.0,  # pad
            10.0,
            10.0,
            10.0,
            10.0,
            10.0,
            10.0,
            9.0,
            8.0,
            7.0,
            6.0,
            5.0,
            12.0,  # jump up -> fast SMA overtakes slow
        ]
        sig = evaluate_signal(closes, 3, 5)
        self.assertIn(sig, ("buy", "sell", "hold"))
        # Last sharp rise should trigger buy for standard windows
        self.assertEqual(sig, "buy")


if __name__ == "__main__":
    unittest.main()
