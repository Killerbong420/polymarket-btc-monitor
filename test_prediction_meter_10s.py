"""Regression gates for read-only ten-second live prediction presentation."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HTML = (ROOT / "terminal.html").read_text(encoding="utf-8")
APP = (ROOT / "app.py").read_text(encoding="utf-8")


class TenSecondPredictionMeterTests(unittest.TestCase):
    def test_live_prediction_is_presented_every_ten_seconds(self):
        self.assertIn("const PREDICTION_DISPLAY_MS=10000;", HTML)
        self.assertIn("setInterval(updateMeter,PREDICTION_DISPLAY_MS)", HTML)
        self.assertNotIn("setInterval(updateMeter,15000)", HTML)
        self.assertIn("if(document.hidden||meterInFlight)return", HTML)
        self.assertIn("document.addEventListener('visibilitychange'", HTML)

    def test_uses_fresh_current_guarded_15m_model_not_locked_checkpoint(self):
        self.assertIn("snapshot&&snapshot.guarded_primary", HTML)
        self.assertIn("snapshot.status!=='running'", HTML)
        self.assertIn("ref.source!=='Polymarket US BTC reference / BRTI'", HTML)
        self.assertIn("ref.status!=='fresh'", HTML)
        self.assertIn("tickAge>15", HTML)
        self.assertIn("updateAge>15", HTML)
        self.assertIn("remaining<=0||remaining>900", HTML)
        self.assertIn("forecast.status!=='available'", HTML)
        self.assertIn("Waiting for fresh verified 15-minute BRTI forecast", HTML)
        self.assertNotIn("const call=byId('final15Signal')", HTML)

    def test_calibrated_history_and_live_forecast_are_not_conflated(self):
        self.assertIn("fetch('/api/direction-probability'", HTML)
        self.assertIn("Historical calibrated UP/DOWN frequency", HTML)
        self.assertIn("separate from live forecast", HTML)
        self.assertIn("no made-up odds", HTML)
        self.assertIn("clearProbabilities();", HTML)

    def test_backend_forecasts_remain_faster_and_no_extra_network_loop(self):
        self.assertRegex(APP, r"(?m)^POLL = 5$")
        self.assertIn("guarded_live=guarded_decision(", APP)
        self.assertIn('state.update(reference_price=ref_public', APP)
        self.assertIn("setInterval(()=>{if(!document.hidden)updateServer()},4000)", HTML)
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshTip()},3000)", HTML)
        meter = HTML.split("const PREDICTION_DISPLAY_MS=10000;",1)[1].split("</script>",1)[0]
        self.assertNotIn("fetch('/api/live'", meter)
        self.assertNotIn("fetch('/api/status'", meter)
        self.assertNotIn("POST", meter)


if __name__ == "__main__":
    unittest.main()
