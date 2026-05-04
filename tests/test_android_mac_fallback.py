import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class AndroidMacFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_zero_mac_is_not_valid_device_mac(self):
        self.assertFalse(main._is_valid_mac("00:00:00:00:00:00"))
        self.assertTrue(main._is_valid_mac("AA:BB:CC:DD:EE:FF"))

    def test_welcome_redirects_to_detect_page_when_mac_missing(self):
        with patch("main._get_busy_activation_macs", return_value=set()), patch(
            "main._pick_qr_mac_fallback", return_value=(None, "no_clients")
        ):
            response = self.client.get(
                "/?router_id=astana_01&cid=test-missing-mac",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/q?router_id=astana_01&cid=test-missing-mac")

    def test_welcome_uses_router_fallback_when_single_mac_found(self):
        with patch("main._get_busy_activation_macs", return_value=set()), patch(
            "main._pick_qr_mac_fallback", return_value=("AA:BB:CC:DD:EE:FF", "single_fresh")
        ):
            response = self.client.get(
                "/?router_id=astana_01&cid=test-fallback-hit",
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            "/?mac=AA%3ABB%3ACC%3ADD%3AEE%3AFF&router_id=astana_01&cid=test-fallback-hit",
        )


if __name__ == "__main__":
    unittest.main()