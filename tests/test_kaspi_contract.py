import unittest

import main


class KaspiContractTests(unittest.TestCase):
    def test_make_and_parse_contract_number(self):
        mac = "AA:BB:CC:11:22:33"
        contract = main.make_contract_number(mac)
        parsed_mac, ok = main.parse_contract_number(contract)

        self.assertTrue(ok)
        self.assertEqual(parsed_mac, mac)

    def test_parse_contract_number_invalid(self):
        parsed_mac, ok = main.parse_contract_number("BAD-CONTRACT")
        self.assertFalse(ok)
        self.assertEqual(parsed_mac, "")


if __name__ == "__main__":
    unittest.main()
