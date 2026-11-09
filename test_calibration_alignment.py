"""Proof that empirical BRTI probabilities compare the same checkpoint minute."""
import copy
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from calibration_alignment import (OFFICIAL_SOURCE, eligible_halfway_history,
                                   match_halfway_live_state)
import app


def baseline(left=430, age=3):
    behavior = {
        "status": "available", "is_reference": True, "source": OFFICIAL_SOURCE,
        "target_gap_pct": .10, "elapsed_seconds": 900-left, "price_age_seconds": age
    }
    reference = {"status": "fresh", "source": OFFICIAL_SOURCE, "age_seconds": age}
    updated = datetime.fromtimestamp(1_800_000_000, timezone.utc).isoformat()
    return behavior, reference, left, updated


class TimeMatchedCalibrationTests(unittest.TestCase):
    def test_only_same_frozen_halfway_minute_is_eligible(self):
        beh, ref, left, ts = baseline()
        self.assertEqual(match_halfway_live_state(beh, ref, left, ts, 1_800_000_002)["status"], "matched")
        for remaining in (899, 451, 389, 180, 1, None):
            self.assertEqual(match_halfway_live_state(beh, ref, remaining, ts, 1_800_000_002)["reason"],
                             "no_same_time_checkpoint_history")
        inconsistent={**beh, "elapsed_seconds": 520}
        self.assertEqual(match_halfway_live_state(inconsistent, ref, 450, ts, 1_800_000_002)["reason"], "market_clock_mismatch")

    def test_reject_stale_official_reference_and_stale_server_cycle(self):
        beh, ref, left, ts = baseline()
        b = dict(beh); b["price_age_seconds"] = 16
        self.assertEqual(match_halfway_live_state(b, ref, left, ts, 1_800_000_002)["reason"], "stale_brti_reference")
        r = dict(ref); r["source"] = "Coinbase spot"
        self.assertEqual(match_halfway_live_state(beh, r, left, ts, 1_800_000_002)["reason"], "missing_verified_brti_reference")
        self.assertEqual(match_halfway_live_state(beh, ref, left, ts, 1_800_000_020)["reason"], "stale_live_cycle")
        self.assertEqual(match_halfway_live_state(beh, ref, left, "not-a-timestamp", 1_800_000_002)["reason"], "missing_fresh_live_cycle")
        b=dict(beh); b["elapsed_seconds"]=250
        self.assertEqual(match_halfway_live_state(b, ref, left, ts, 1_800_000_002)["reason"], "market_clock_mismatch")

    def test_historical_labels_must_match_official_time_and_source(self):
        beh, ref, left, ts = baseline()
        snap={"seconds_left":left,"target_behavior":beh,"reference_price":ref}
        self.assertTrue(eligible_halfway_history(snap))
        for change in ({"seconds_left":690},
                       {"target_behavior":{**beh,"source":"exchange spot"}},
                       {"target_behavior":{**beh,"elapsed_seconds":50}},
                       {"reference_price":{**ref,"status":"stale"}},
                       {"reference_price":{**ref,"age_seconds":-1}}):
            self.assertFalse(eligible_halfway_history({**snap,**change}))

    def test_endpoint_rejects_time_mismatch_before_query_or_cache(self):
        beh, ref, left, ts = baseline(left=690)
        saved = {"target_behavior":beh, "reference_price":ref,
                 "market":"btc-updown-15m-2026-11-09-2300z",
                 "seconds_left":left, "updated":ts}
        with patch.object(app,"state",saved), patch("app.time.time",return_value=1_800_000_002),\
             patch.object(app,"iter_query_rows",side_effect=AssertionError("unexpected query")):
            result=app.direction_probability()
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["reason"],"no_same_time_checkpoint_history")

    def test_endpoint_skips_wrong_historical_checkpoints(self):
        beh, ref, left, ts = baseline()
        present = {"target_behavior":beh, "reference_price":ref,
                   "market":"btc-updown-15m-2026-11-09-2300z",
                   "seconds_left":left, "updated":ts}
        hist_ok = {"target_behavior":beh,"reference_price":ref,"seconds_left":left}
        hist_wrong = {**hist_ok,"seconds_left":700}
        hist_stale = {**hist_ok,"reference_price":{**ref,"source":"Coinbase spot"}}
        rows=[("btc-updown-15m-2026-11-08-2300z",json.dumps(hist_ok),"UP"),
              ("btc-updown-15m-2026-11-07-2300z",json.dumps(hist_wrong),"DOWN"),
              ("btc-updown-15m-2026-11-06-2300z",json.dumps(hist_stale),"DOWN")]
        with patch.object(app,"state",present),patch("app.time.time",return_value=1_800_000_002),\
             patch.object(app,"iter_query_rows",return_value=rows),\
             patch.object(app,"_calibrate_probability",return_value={"status":"unavailable","samples":1}) as cal:
            app._probability_cache.update(at=0.0,market=None,gap=None,data=None)
            try:
                result=app.direction_probability()
            finally:
                app._probability_cache.update(at=0.0,market=None,gap=None,data=None)
        self.assertEqual(result["checkpoint"],"HALFWAY")
        self.assertEqual(result["seconds_left_band"],[390,450])
        self.assertEqual(result["alignment"],"same_one_minute_checkpoint_band")
        self.assertEqual(cal.call_args.args[0],[("btc-updown-15m-2026-11-08-2300z",.1,"UP")])


if __name__ == "__main__":
    unittest.main()
