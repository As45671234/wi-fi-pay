import os
import sqlite3
import tempfile
import unittest

import main


class KaspiIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.orig_db_path = main.DB_PATH
        main.DB_PATH = self.tmp.name
        main.init_db()

        conn = main.get_db()
        try:
            conn.execute(
                """
                INSERT INTO kaspi_orders (
                    local_order_id, contract_number, external_order_ref,
                    mac_address, router_id, amount, minutes,
                    kaspi_order_id, kaspi_status, is_activated, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    "local_1",
                    "A13AABBCCDDEEFF1234567890ABCD",
                    "A13AABBCCDDEEFF1234567890ABCD",
                    "AA:BB:CC:DD:EE:FF",
                    "astana_01",
                    500,
                    120,
                    "order_1",
                    "PAID",
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        main.DB_PATH = self.orig_db_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_claim_activation_only_once(self):
        contract = "A13AABBCCDDEEFF1234567890ABCD"
        first = main._claim_kaspi_activation(contract)
        second = main._claim_kaspi_activation(contract)

        self.assertTrue(first)
        self.assertFalse(second)

    def test_process_paid_is_idempotent(self):
        contract = "A13AABBCCDDEEFF1234567890ABCD"
        calls = {"count": 0}

        original_activate = main._activate_kaspi_order
        try:
            def fake_activate(_contract):
                calls["count"] += 1
                return True

            main._activate_kaspi_order = fake_activate

            main._process_kaspi_paid(contract)
            main._process_kaspi_paid(contract)

            self.assertEqual(calls["count"], 1)

            conn = main.get_db()
            try:
                row = conn.execute(
                    "SELECT is_activated FROM kaspi_orders WHERE contract_number = ?",
                    (contract,),
                ).fetchone()
            finally:
                conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(int(row[0]), 1)
        finally:
            main._activate_kaspi_order = original_activate


if __name__ == "__main__":
    unittest.main()
