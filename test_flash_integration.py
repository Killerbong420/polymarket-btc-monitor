"""Integration checks for immediate observed BRTI shock warnings and safe audit wiring."""
import inspect
import unittest
from unittest.mock import patch

import app
from market_shock import BRTI_SOURCE


class FlashIntegrationTests(unittest.TestCase):
    def test_live_endpoint_reads_existing_one_second_chart_memory(self):
        start = 1_700_000_000
        points = [[start+i, 65000-2*i] for i in range(330)]
        with patch.dict(app.state, {
            "status": "running",
            "reference_price": {"status": "fresh", "source": BRTI_SOURCE},
            "external": {"market_shock": {"status": "available", "level": "normal"}},
        }, clear=True):
            with patch.object(app, "chart_history_snapshot", return_value={
                "status": "fresh", "brti_points": points,
            }) as read_chart:
                with patch.object(app.time, "time", return_value=start+329):
                    response = app.lightweight_live()
        read_chart.assert_called_once_with("5m", now=start+329)
        self.assertEqual(response["flash_move"]["level"], "flash_selloff")
        self.assertEqual(response["external"]["flash_move"]["direction"], "DOWN")
        self.assertEqual(response["external"]["market_shock"]["level"], "normal")

    def test_unverified_reference_identity_cannot_show_flash_alert(self):
        start = 1_700_000_000
        points = [[start+i, 65000-2*i] for i in range(330)]
        with patch.dict(app.state, {
            "status": "running",
            "reference_price": {"status": "fresh", "source": "Coinbase BTC-USD"},
            "external": {},
        }, clear=True):
            with patch.object(app, "chart_history_snapshot", return_value={
                "status": "fresh", "brti_points": points,
            }):
                with patch.object(app.time, "time", return_value=start+329):
                    response = app.lightweight_live()
        self.assertEqual(response["flash_move"]["status"], "unavailable")

    def test_frozen_checkpoint_keeps_official_target_safety_gate(self):
        source = inspect.getsource(app.capture_checkpoint)
        self.assertIn('target_validation=frozen.get("target_validation")', source)

    def test_flash_is_not_used_to_override_direction_or_target(self):
        source = inspect.getsource(app.loop)
        self.assertIn('d["flash_move"]=flash_move', source)
        self.assertIn('guarded_decision(price_path,ref_public,target,left15,', source)
        self.assertNotIn('target=flash_move', source)


if __name__ == "__main__":
    unittest.main()
