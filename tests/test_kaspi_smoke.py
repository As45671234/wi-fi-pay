import os
import tempfile
import unittest

import main


class KaspiSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.orig_db_path = main.DB_PATH
        main.DB_PATH = self.tmp.name
        main.init_db()

    def tearDown(self):
        main.DB_PATH = self.orig_db_path
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_local_create_fake_paid_and_single_activation(self):
        mac = "AA:BB:CC:AA:BB:CC"
        contract = main.make_contract_number(mac)

        conn = main.get_db()
        try:
            conn.execute(
                """
                INSERT INTO kaspi_orders (
                    local_order_id, contract_number, external_order_ref,
                    mac_address, router_id, amount, minutes,
                    kaspi_status, is_activated, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    "local_smoke_1",
                    contract,
                    contract,
                    mac,
                    "astana_01",
                    500,
                    120,
                    "CREATED",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        main._upsert_kaspi_remote_state(
            contract_number=contract,
            kaspi_order_id="kaspi_paid_1",
            kaspi_status="PAID",
            paid_at=None,
        )

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
                    "SELECT kaspi_status, is_activated FROM kaspi_orders WHERE contract_number = ?",
                    (contract,),
                ).fetchone()
            finally:
                conn.close()

            self.assertIsNotNone(row)
            self.assertEqual((row[0] or "").upper(), "PAID")
            self.assertEqual(int(row[1]), 1)
        finally:
            main._activate_kaspi_order = original_activate


if __name__ == "__main__":
    unittest.main()
