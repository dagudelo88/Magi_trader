"""bot_orders table persistence and API helper queries."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

import database as db


class TestSyncOrdersFromLogs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self._tmp.close()
        self._patch = patch.object(db, "DB_PATH", self._tmp.name)
        self._patch.start()
        db._pool = None
        db.init_db()

    def tearDown(self) -> None:
        db._pool = None
        self._patch.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_backfill_from_legacy_buy_log_line(self) -> None:
        conn = db.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO bot_logs (bot_id, created_at, level, execution_mode, message)
                VALUES (?, ?, 'info', 'testnet', ?)
                """,
                (
                    "1",
                    1_700_000_000_000,
                    "BUY market order placed id=20499149 quoteOrderQty=200.0",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        n = db.sync_bot_orders_from_logs("1", "BTC/USDT")
        self.assertEqual(n, 1)
        stats, orders = db.fetch_bot_orders_panel("1")
        self.assertEqual(stats["total_orders"], 1)
        self.assertEqual(orders[0]["exchange_order_id"], "20499149")
        self.assertEqual(orders[0]["cost"], 200.0)
        self.assertEqual(orders[0]["side"], "buy")

        n2 = db.sync_bot_orders_from_logs("1", "BTC/USDT")
        self.assertEqual(n2, 0)


class TestDatabaseBotOrders(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self._tmp.close()
        self._patch = patch.object(db, "DB_PATH", self._tmp.name)
        self._patch.start()
        db._pool = None
        db.init_db()
        conn = db.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO bots (bot_id, name, symbol, strategy, status, execution_mode, created_at)
                VALUES ('1', 'Test bot', 'BTC/USDT', 'sma_cross', 'stopped', 'testnet', 1)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self) -> None:
        db._pool = None
        self._patch.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_record_and_fetch_orders(self) -> None:
        stats, orders = db.fetch_bot_orders_panel("1")
        self.assertEqual(stats["total_orders"], 0)
        self.assertEqual(orders, [])

        db.record_bot_order(
            "1",
            "testnet",
            {
                "id": "99001",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "amount": 0.001,
                "cost": 50.0,
                "filled": 0.001,
                "status": "closed",
            },
        )
        stats, orders = db.fetch_bot_orders_panel("1")
        self.assertEqual(stats["total_orders"], 1)
        self.assertEqual(stats["buy_count"], 1)
        self.assertEqual(stats["sell_count"], 0)
        self.assertIsNotNone(stats["last_order_at_ms"])
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["exchange_order_id"], "99001")
        self.assertEqual(orders[0]["side"], "buy")
        self.assertEqual(orders[0]["display_price"], 50_000.0)
        self.assertEqual(orders[0]["display_status"], "CLOSED")

    def test_order_helpers_filter_by_execution_mode(self) -> None:
        db.record_bot_order(
            "1",
            "testnet",
            {
                "id": "tn-1",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "amount": 1.0,
                "cost": 100.0,
                "filled": 1.0,
                "status": "closed",
            },
        )
        db.record_bot_order(
            "1",
            "live",
            {
                "id": "lv-1",
                "symbol": "BTC/USDT",
                "side": "sell",
                "type": "market",
                "amount": 1.0,
                "cost": 110.0,
                "filled": 1.0,
                "status": "closed",
            },
        )

        stats_testnet, orders_testnet = db.fetch_bot_orders_panel("1", mode="testnet")
        stats_live, orders_live = db.fetch_bot_orders_panel("1", mode="live")
        stats_both, _ = db.fetch_bot_orders_panel("1")

        self.assertEqual(stats_testnet["total_orders"], 1)
        self.assertEqual(orders_testnet[0]["exchange_order_id"], "tn-1")
        self.assertEqual(stats_live["total_orders"], 1)
        self.assertEqual(orders_live[0]["exchange_order_id"], "lv-1")
        self.assertEqual(stats_both["total_orders"], 2)
        self.assertEqual(len(db.fetch_bot_orders_chronological("1", mode="testnet")), 1)
        self.assertEqual(len(db.fetch_bot_orders_chronological("1", mode="live")), 1)

    def test_promote_to_live_requires_explicit_capital(self) -> None:
        with self.assertRaises(ValueError):
            db.promote_bot_to_live("1", initial_capital_quote=0)

        promoted = db.promote_bot_to_live(
            "1",
            initial_capital_quote=125.0,
        )
        self.assertEqual(promoted["execution_mode"], "live")
        self.assertEqual(promoted["capital_source"], "budget")
        self.assertEqual(promoted["live_initial_capital_quote"], 125.0)

    def test_capital_flows_are_signed_and_summed(self) -> None:
        db.record_bot_capital_flow("1", "live", 25.0, "deposit", "top up")
        db.record_bot_capital_flow("1", "live", 10.0, "withdrawal", "cash out")
        db.record_bot_capital_flow("1", "testnet", 99.0, "deposit", "ignore")

        flows = db.get_bot_capital_flows("1", "live")
        self.assertEqual(len(flows), 2)
        self.assertAlmostEqual(db.get_bot_net_capital_flow("1", "live"), 15.0)
        self.assertAlmostEqual(
            db.get_bot_net_capital_flow("1", "testnet"),
            99.0,
        )

    def test_voter_feedback_stores_execution_mode(self) -> None:
        db.insert_voter_feedback(
            {
                "bot_id": "1",
                "execution_mode": "live",
                "timestamp": 1_700_000_000_000,
                "target_asset": "BTC/USDT",
                "ensemble_signal": "buy",
                "voter_name": "sma_cross",
                "voter_signal": "buy",
            }
        )
        rows = db.get_latest_voter_signals("1", mode="live")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["execution_mode"], "live")
        self.assertEqual(db.get_latest_voter_signals("1", mode="testnet"), [])

    def test_fork_bot_preserves_source_history(self) -> None:
        conn = db.get_db_connection()
        try:
            conn.execute(
                """
                INSERT INTO bot_logs (bot_id, created_at, level, execution_mode, message)
                VALUES ('1', 1, 'info', 'testnet', 'hello')
                """
            )
            conn.commit()
        finally:
            conn.close()

        db.record_bot_order(
            "1",
            "testnet",
            {
                "id": "991",
                "symbol": "BTC/USDT",
                "side": "buy",
                "type": "market",
                "amount": 0.001,
                "cost": 40.0,
                "filled": 0.001,
                "status": "closed",
            },
        )
        r = db.fork_bot("1", name="Fresh runner")
        new_id = r["new_bot_id"]
        self.assertNotEqual(new_id, "1")
        self.assertEqual(r["name"], "Fresh runner")

        stats_old, _ = db.fetch_bot_orders_panel("1")
        self.assertEqual(stats_old["total_orders"], 1)
        conn = db.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS c FROM bot_logs WHERE bot_id = ?", ("1",))
            self.assertEqual(cur.fetchone()["c"], 1)
        finally:
            conn.close()

        stats_new, orders_new = db.fetch_bot_orders_panel(new_id)
        self.assertEqual(stats_new["total_orders"], 0)
        self.assertEqual(orders_new, [])

    def test_fork_bot_custom_params_json(self) -> None:
        custom = '{"fast_period": 5, "initial_budget_quote": 888}'
        r = db.fork_bot("1", strategy_params_json=custom)
        conn = db.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT strategy_params_json FROM bots WHERE bot_id = ?",
                (r["new_bot_id"],),
            )
            self.assertEqual(cur.fetchone()["strategy_params_json"], custom)
        finally:
            conn.close()

    def test_risk_settings_copy_on_fork_and_delete(self) -> None:
        db.upsert_bot_risk_settings(
            "1",
            {
                "base_risk_pct": 1.5,
                "dynamic_tiers": [{"min_score": None, "max_score": None, "multiplier": 1.0}],
                "daily_loss_limit_pct": 5,
                "max_drawdown_pct": 12,
                "consecutive_loss_limit": 10,
                "enable_daily_loss_limit": True,
                "enable_drawdown_protection": True,
                "enable_consecutive_loss": True,
                "enable_dynamic_sizing": True,
                "enable_volatility_pause": False,
                "volatility_threshold": None,
                "drawdown_action": "reduce",
                "drawdown_reduce_factor": 0.5,
            },
        )
        r = db.fork_bot("1", name="Risk copy")
        copied = db.get_bot_risk_settings(r["new_bot_id"])
        self.assertIsNotNone(copied)
        self.assertEqual(copied["base_risk_pct"], 1.5)

        db.delete_bot(r["new_bot_id"])
        self.assertIsNone(db.get_bot_risk_settings(r["new_bot_id"]))


if __name__ == "__main__":
    unittest.main()
