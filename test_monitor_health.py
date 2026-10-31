"""Regression tests for health alerts after intentionally disabling live AI."""
import unittest

from monitor_health import checks


class DisabledAIHealthTests(unittest.TestCase):
    def test_intentionally_disabled_ai_does_not_raise_missing_key_warning(self):
        alerts = checks({"ai_disabled": True}, {"status": "not_configured"}, True, now=0)
        self.assertNotIn("AI", {alert["code"] for alert in alerts})

    def test_intentionally_disabled_ai_does_not_raise_stale_warning(self):
        alerts = checks({"ai_disabled": True, "market": "current"},
                        {"status": "ready", "market": "previous", "updated_epoch": 0},
                        True, now=500)
        self.assertNotIn("AI_STALE", {alert["code"] for alert in alerts})

    def test_enabled_ai_still_reports_missing_configuration(self):
        alerts = checks({"ai_disabled": False}, {"status": "not_configured"}, True, now=0)
        self.assertIn("AI", {alert["code"] for alert in alerts})

    def test_enabled_ai_still_reports_stale_prediction(self):
        alerts = checks({"market": "current"},
                        {"status": "ready", "market": "previous", "updated_epoch": 0},
                        True, now=500)
        self.assertIn("AI_STALE", {alert["code"] for alert in alerts})


if __name__ == "__main__":
    unittest.main()
