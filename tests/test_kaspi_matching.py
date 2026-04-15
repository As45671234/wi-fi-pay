import os
import tempfile
import unittest

import main


class KaspiMatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.orig_db_path = main.DB_PATH
        main.DB_PATH = self.tmp.name
        main.init_db()

        self.contract = main.make_contract_number("AA:BB:CC:DD:EE:11")

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
                    "local_match",
                    self.contract,
                    self.contract,
                    "AA:BB:CC:DD:EE:11",
                    "astana_01",
                    1000,
                    240,
                    "CREATED",
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

    def test_match_by_contract_and_amount(self):
        kaspi_order = {
            "contract_number": self.contract,
            "amount": 1000,
            "kaspi_status": "PAID",
            "kaspi_order_id": "kz_order_1",
        }
        matched = main._match_kaspi_order(kaspi_order)
        self.assertEqual(matched, self.contract)

    def test_no_match_when_amount_differs(self):
        kaspi_order = {
            "contract_number": self.contract,
            "amount": 999,
            "kaspi_status": "PAID",
            "kaspi_order_id": "kz_order_2",
        }
        matched = main._match_kaspi_order(kaspi_order)
        self.assertIsNone(matched)


if __name__ == "__main__":
    unittest.main()
