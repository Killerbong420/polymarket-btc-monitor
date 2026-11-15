"""Early sell-off detection and one-second read-only live endpoint regression."""
import inspect
import unittest
from unittest.mock import patch

import app
from market_shock import measure_brti_rapid_move

SOURCE = "Polymarket US BTC reference / BRTI"
EPOCH = 1_700_000_000


def ref(prices, missing=()):
    return {
        "status": "fresh",
        "source": SOURCE,
        "points": [(EPOCH+i, p) for i, p in enumerate(prices) if i not in missing],
    }


class EarlyBRTIAlertTests(unittest.TestCase):
    def test_one_minute_fall_detected_without_five_minute_history(self):
        prices = [64000]*79 + [64000 - 5*i for i in range(61)]
        result = measure_brti_rapid_move(ref(prices), now_epoch=EPOCH+139)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertEqual(result["change_usd"], -300.0)
        self.assertEqual(result["change_60s_usd"], -300.0)
        self.assertEqual(result["phase"], "early_selloff")
        self.assertLess(result["lookback_seconds"], 120)

    def test_one_minute_warning_when_full_five_minute_net_move_is_small(self):
        prices = [64000]*260 + [64000 - 5*i for i in range(61)]
        result = measure_brti_rapid_move(ref(prices), now_epoch=EPOCH+320)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertLess(result["lookback_seconds"], 120)

    def test_five_minute_500_drop_still_triggers(self):
        prices = [65000 - 2*i for i in range(330)]
        result = measure_brti_rapid_move(ref(prices), now_epoch=EPOCH+329)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertGreaterEqual(abs(result["change_usd"]), 500)
        self.assertGreater(result["lookback_seconds"], 280)

    def test_short_rally_not_misclassified_as_selloff(self):
        prices = [64000]*79 + [64000 + 6*i for i in range(61)]
        result = measure_brti_rapid_move(ref(prices), now_epoch=EPOCH+139)
        self.assertEqual(result["kind"], "rapid_rally")

    def test_incomplete_or_stale_minute_never_generates_alert(self):
        prices = [64000]*79 + [64000 - 5*i for i in range(61)]
        self.assertEqual(measure_brti_rapid_move(ref(prices,missing=set(range(95,115))),now_epoch=EPOCH+139)["status"],"unavailable")
        self.assertEqual(measure_brti_rapid_move(ref(prices),now_epoch=EPOCH+170)["status"],"unavailable")

    def test_live_api_uses_latest_memory_not_slow_checkpoint_snapshot(self):
        prices = [64000]*79 + [64000 - 5*i for i in range(61)]
        points = ref(prices)["points"]
        with patch.dict(app.state, {
            "status":"running",
            "market":"cpc-btc-updown-15m-2026-11-15-0100z",
            "reference_price":{"status":"fresh","source":SOURCE},
            "brti_rapid_move":{"status":"unavailable"},
        }, clear=True):
            with patch.object(app,"recent_brti_points",return_value=points) as recent:
                with patch.object(app.time,"time",return_value=EPOCH+139):
                    result = app.lightweight_live()
        recent.assert_called_once_with(seconds=330,now=EPOCH+139)
        self.assertEqual(result["brti_rapid_move"]["kind"],"rapid_selloff")
        self.assertEqual(result["brti_rapid_move"]["market"],"cpc-btc-updown-15m-2026-11-15-0100z")

    def test_market_identity_is_required_for_fast_api_alert(self):
        prices = [64000]*79 + [64000 - 5*i for i in range(61)]
        with patch.dict(app.state,{
            "status":"market unavailable",
            "market":None,
            "reference_price":{"status":"fresh","source":SOURCE},
        },clear=True):
            with patch.object(app,"recent_brti_points",return_value=ref(prices)["points"]) as recent:
                with patch.object(app.time,"time",return_value=EPOCH+139):
                    result = app.lightweight_live()
        recent.assert_not_called()
        self.assertEqual(result["brti_rapid_move"]["status"],"unavailable")

    def test_frozen_guarded_checkpoint_keeps_target_validation(self):
        source = inspect.getsource(app.capture_checkpoint)
        self.assertIn('target_validation=frozen.get("target_validation")',source)


if __name__=="__main__":
    unittest.main()
