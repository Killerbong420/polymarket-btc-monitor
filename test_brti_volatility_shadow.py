"""BRTI theoretical shadow is future-facing, non-trading, and scoreable."""
import json
import unittest
from brti_volatility_shadow import (VERSION, brti_volatility_shadow, evaluate_shadow)


def observed(price=100.0, age=2.0):
    return {"source": "Polymarket US BTC reference / BRTI",
            "status": "fresh", "price": price, "age_seconds": age}


def observed_path(target=99.0, vol=2.0):
    return {"status": "available", "quality": "OBSERVED", "target_usd": target,
            "volatility_reference_usd_per_min": vol}


class VolatilityShadowTests(unittest.TestCase):
    def test_theoretical_probability_for_60s_mean_not_spot_expiry(self):
        actual = brti_volatility_shadow(observed(), observed_path(), 99, 100)
        self.assertEqual(actual["status"], "available")
        self.assertEqual(actual["source"], VERSION)
        self.assertEqual(actual["effective_variance_seconds"], 60.0)  # T-40
        self.assertEqual(actual["direction"], "UP")
        self.assertGreater(actual["model_up_probability"], 0.5)
        self.assertAlmostEqual(actual["model_up_probability"]+actual["model_down_probability"], 1)
        self.assertFalse(actual["probabilities_calibrated"])
        self.assertFalse(actual["orders_enabled"])

    def test_longer_horizon_near_target_reduces_theoretical_confidence(self):
        soon = brti_volatility_shadow(observed(), observed_path(), 99, 100)
        later = brti_volatility_shadow(observed(), observed_path(), 99, 600)
        self.assertLess(later["model_up_probability"], soon["model_up_probability"])
        self.assertEqual(brti_volatility_shadow(observed(), observed_path(101), 101, 100)["direction"], "DOWN")

    def test_fail_closed_when_reference_target_or_volatility_is_untrusted(self):
        self.assertEqual(brti_volatility_shadow({"source":"Coinbase","status":"fresh","price":100}, observed_path(), 99, 500)["direction"], "WAIT")
        self.assertEqual(brti_volatility_shadow(observed(age=16), observed_path(), 99, 500)["status"], "unavailable")
        self.assertEqual(brti_volatility_shadow(observed(age=-1), observed_path(), 99, 500)["status"], "unavailable")
        self.assertEqual(brti_volatility_shadow(observed(), observed_path(target=99.1), 99, 500)["status"], "unavailable")
        self.assertEqual(brti_volatility_shadow(observed(), observed_path(), 99, 500, {"material":True})["status"], "unavailable")
        low = observed_path()
        low["quality"] = "LIMITED"
        self.assertEqual(brti_volatility_shadow(observed(), low, 99, 500)["status"], "unavailable")
        self.assertEqual(brti_volatility_shadow(observed(), observed_path(vol=0), 99, 500)["status"], "unavailable")
        self.assertEqual(brti_volatility_shadow(observed(), observed_path(), 99, 60)["status"], "unavailable")

    def test_no_direction_when_exactly_centered(self):
        zero = brti_volatility_shadow(observed(price=100), observed_path(target=100), 100, 450)
        self.assertEqual(zero["direction"], "WAIT")
        self.assertEqual(zero["model_up_probability"], .5)

    def test_score_only_officially_settled_forward_rows_and_dedupes(self):
        def row(m, p, win, checkpoint="HALFWAY", version=VERSION):
            call = {"source": version, "status": "available", "model_up_probability": p}
            return (m,checkpoint,json.dumps({"BRTI_VOL_SHADOW":call}),win)
        report = evaluate_shadow([row("m1",.75,"UP"), row("m1",.75,"UP"),
                                  row("m2",.25,"DOWN"), row("m3",.8,None),
                                  row("m4",.99,"DOWN",version="old")])
        self.assertEqual(len(report["cohorts"]), 1)
        cohort = report["cohorts"][0]
        self.assertEqual(cohort["officially_settled_rounds"], 2)
        self.assertEqual(cohort["model_brier"], .0625)
        self.assertFalse(cohort["sample_sufficient_for_review"])
        self.assertFalse(report["auto_promote"])


if __name__ == "__main__":
    unittest.main()
