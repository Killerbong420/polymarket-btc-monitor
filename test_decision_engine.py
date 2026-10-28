"""Prospective guarded path, reversal, reconciliation, and scoring tests."""
import json
import unittest

from decision_engine import guarded_decision, reversal_evidence, manual_entry_review
from forward_eval import evaluate_guarded
from forecast_guard import validate_official_target


def reference():
    return {"status": "fresh", "source": "Polymarket US BTC reference / BRTI",
            "age_seconds": 2,
            "points": [(t, 100 + .01*t) for t in range(180)]}


def path():
    return {"status": "available", "quality": "OBSERVED",
            "target_usd": 100, "current_reference_usd": 100,
            "forecast_final_mean_usd": 101,
            "volatility_reference_usd_per_min": .4,
            "downside_scenario_usd": 98, "upside_scenario_usd": 103}


class GuardedDecisionTests(unittest.TestCase):
    def test_reversal_uses_observed_only(self):
        observed = reversal_evidence(reference(), 450)
        self.assertEqual(observed["status"], "observed")
        self.assertFalse(observed["reversal"])
        missing = reference()
        missing["points"] = missing["points"][-10:]
        self.assertEqual(reversal_evidence(missing, 450)["status"], "unavailable")

    def test_far_horizon_shrinks_not_copies_current(self):
        early=guarded_decision(path(), reference(), 100, 720)
        halfway=guarded_decision(path(), reference(), 100, 450)
        late=guarded_decision(path(), reference(), 100, 180)
        self.assertEqual(early["status"], "available")
        self.assertEqual(early["forecast_final_mean_usd"], 100.35)
        self.assertEqual(halfway["forecast_final_mean_usd"], 100.75)
        self.assertEqual(late["forecast_final_mean_usd"], 101)
        self.assertEqual(early["direction"], "UP")

    def test_fresh_official_data_required(self):
        stale=reference()
        stale["status"]="stale"
        self.assertEqual(guarded_decision(path(),stale,100,450)["direction"], "WAIT")
        self.assertEqual(guarded_decision(path(),reference(),101,450)["status"], "unavailable")
        self.assertEqual(guarded_decision({**path(),"quality":"LIMITED"},reference(),100,450)["status"], "unavailable")

    def test_ai_adjustment_bounded_and_optional(self):
        base=guarded_decision(path(),reference(),100,180)
        distant=guarded_decision(path(),reference(),100,180,ai_forecast=200)
        close=guarded_decision(path(),reference(),100,180,ai_forecast=102)
        self.assertEqual(base["forecast_final_mean_usd"],distant["forecast_final_mean_usd"])
        self.assertLessEqual(abs(close["forecast_final_mean_usd"]-base["forecast_final_mean_usd"]),.05)
        self.assertFalse(close["calibrated_probability"])

    def test_reversal_direction_adjustment_bounded(self):
        signal={"status":"observed","reversal":True,"short_usd_per_min":-10,
                "long_usd_per_min":2}
        result=guarded_decision(path(),reference(),100,180,reversal=signal)
        self.assertEqual(result["reversal_adjustment_usd"],-.1)

    def test_entry_review_is_cost_aware_and_manual(self):
        candidate=guarded_decision(path(),reference(),100,180)
        quote={"status":"available","market":"m","up_entry":.88,"down_entry":.12}
        expensive=manual_entry_review(candidate,quote,"m")
        self.assertEqual(expensive["action"],"AVOID_HIGH_PRICE")
        self.assertFalse(expensive["expected_value_established"])
        quote["up_entry"]=.55
        reviewed=manual_entry_review(candidate,quote,"m")
        self.assertIn(reviewed["action"],("MANUAL_REVIEW_ONLY","INSUFFICIENT_MARGIN"))
        self.assertFalse(reviewed["orders_enabled"])
        self.assertEqual(manual_entry_review(candidate,quote,"different")["status"],"unavailable")

    def test_target_difference_classified_but_not_used_as_price(self):
        minor=validate_official_target(80000,{"opening_average":80005,"opening_average_samples":60})
        major=validate_official_target(80000,{"opening_average":80050,"opening_average_samples":60})
        self.assertEqual(minor["classification"],"RECONSTRUCTION_VARIANCE")
        self.assertFalse(minor["material"])
        self.assertTrue(major["material"])
        self.assertEqual(major["official_target_usd"],80000)


class ForwardEvaluationTests(unittest.TestCase):
    def test_forward_only_matched_settlements_and_no_backfill(self):
        call={"source":"guarded-path-primary-v1","status":"available",
              "direction":"UP","forecast_final_mean_usd":101,
              "confidence":"MIXED","entry_review":{"action":"MANUAL_REVIEW_ONLY",
              "estimated_cost":{"status":"estimated","net_if_win_usd":40,"net_if_loss_usd":-60}}}
        orig={"GUARDED_PRIMARY":call,"AI":{"source":"AI_REVIEW","direction":"DOWN",
              "forecast_final_mean_usd":99},"PRICE_PATH":{"direction":"UP",
              "forecast_final_mean_usd":101}}
        row=("marketA","HALFWAY","timestamp","{}",json.dumps(orig),"UP",101)
        pending=("marketB","HALFWAY","timestamp","{}",json.dumps(orig),None,None)
        no_candidate=("marketC","HALFWAY","timestamp","{}","{}","UP",101)
        res=evaluate_guarded([row,pending,no_candidate,row])
        g=res["results"][0]
        self.assertEqual(g["calls"],2)
        self.assertEqual(g["settled"],1)
        self.assertEqual(g["wins"],1)
        self.assertEqual(g["paired_ai_rounds"],1)
        self.assertEqual(g["raw_ai_paired_pct"],0)
        self.assertEqual(g["guarded_vs_ai_pct"],100)
        self.assertEqual(g["hypothetical_hold_net_usd"],40)


if __name__ == "__main__":
    unittest.main()
