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
        db.init_db()

    def tearDown(self) -> None:
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
        db.init_db()

    def tearDown(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
