"""Prospective shadow and missing-evidence regression tests."""
import json
import unittest
from unittest.mock import patch

from ai_confirmation import final_price_direction, numeric_forecast_required
from forecast_guard import anchored_shadow_forecast, validate_official_target
import app


def reference():
    return {"status":"fresh", "source":"Polymarket US BTC reference / BRTI", "price":100.0}


def path():
    return {"status":"available", "quality":"OBSERVED",
            "target_usd":100.0, "forecast_final_mean_usd":100.0,
            "downside_scenario_usd":98.0, "upside_scenario_usd":102.0}


class ForecastGuardTests(unittest.TestCase):
    def test_official_target_is_never_replaced(self):
        observed={"opening_average":100.08,"opening_average_samples":60}
        warning=validate_official_target(100.0,observed)
        self.assertEqual(warning["status"],"mismatch")
        self.assertEqual(warning["official_target_usd"],100.0)
        self.assertEqual(warning["difference_usd"],.08)
        self.assertTrue(warning["official_target_authoritative"])
        self.assertEqual(validate_official_target(100.0,{"opening_average":100.1,"opening_average_samples":59})["status"],"unavailable")
        self.assertEqual(validate_official_target(100.0,{"opening_average":100.01,"opening_average_samples":60})["status"],"match")

    def test_shadow_does_not_override_AI(self):
        raw_ai=101.0
        p=path()
        shadow=anchored_shadow_forecast(raw_ai,p,reference(),100.0)
        self.assertEqual(shadow["status"],"available")
        self.assertEqual(shadow["version"],"anchored-brti-price-shadow-v2")
        self.assertEqual(shadow["forecast_final_mean_usd"],100.04)
        self.assertAlmostEqual(shadow["weight_ai"],0.04)
        self.assertAlmostEqual(shadow["weight_path"],0.96)
        self.assertEqual(shadow["projected_direction"],"UP")
        self.assertEqual(raw_ai,101.0)
        self.assertEqual(p["forecast_final_mean_usd"],100.0)

    def test_shadow_clamps_wild_excursions_and_needs_trustworthy_path(self):
        shadow=anchored_shadow_forecast(130.,path(),reference(),100.)
        self.assertEqual(shadow["status"],"available")
        self.assertTrue(shadow["ai_was_clipped"])
        self.assertEqual(shadow["forecast_final_mean_usd"],100.0)
        self.assertEqual(shadow["weight_ai"],0.0)
        p=path();p["target_usd"]=100.04
        self.assertEqual(anchored_shadow_forecast(130.,p,reference(),100.)["status"],"unavailable")
        p=path();p["quality"]="LIMITED"
        self.assertEqual(anchored_shadow_forecast(130.,p,reference(),100.)["status"],"unavailable")
        stale=reference();stale["status"]="stale"
        self.assertEqual(anchored_shadow_forecast(130.,path(),stale,100.)["status"],"unavailable")

    def test_shadow_respects_scenario_distance_and_no_wild_extrapolation(self):
        p=path()
        p["forecast_final_mean_usd"]=101.0
        p["upside_scenario_usd"]=103.0
        shadow=anchored_shadow_forecast(106.0,p,reference(),100.0)
        self.assertEqual(shadow["forecast_final_mean_usd"],101.0)
        self.assertTrue(shadow["ai_was_clipped"])
        self.assertEqual(shadow["projected_direction"],"UP")
        self.assertEqual(shadow["ai_path_disagreement_usd"],5.0)
        self.assertEqual(shadow["weight_ai"],0.0)

    def test_unavailable_target_or_index_does_not_reject_ai_call(self):
        fresh={"reference_price":reference()}
        self.assertTrue(numeric_forecast_required(fresh,100.))
        self.assertEqual(final_price_direction({"forecast_final_mean_usd":101.},fresh,None),(None,None))
        self.assertEqual(final_price_direction({"forecast_final_mean_usd":None},fresh,None),(None,None))
        stale={"reference_price":{**reference(),"status":"stale"},
               "settlement_forecast":path()}
        self.assertFalse(numeric_forecast_required(stale,100.))
        self.assertEqual(final_price_direction({"forecast_final_mean_usd":101.},stale,100.),(None,None))
        self.assertEqual(final_price_direction({"forecast_final_mean_usd":101.},fresh,100.),(101.,"UP"))
        with self.assertRaisesRegex(ValueError,"ai_forecast_missing"):
            final_price_direction({"forecast_final_mean_usd":None},fresh,100.)

    def test_shadow_metrics_compare_only_identical_settled_checkpoints(self):
        calls={"AI":{"source":"AI_REVIEW","forecast_final_mean_usd":110.0},
               "PRICE_PATH":{"forecast_final_mean_usd":101.0},
               "ANCHORED_SHADOW":{"forecast_final_mean_usd":102.0}}
        snapshot={"settlement_forecast":{"current_reference_usd":100.0}}
        row=("HALFWAY",json.dumps(snapshot),json.dumps(calls),100.5)
        with patch.object(app,"iter_query_rows",return_value=[row]):
            report=app.numeric_price_forecast_report()
        counts={v["source"]:v["graded"] for v in report["results"]}
        self.assertEqual(counts["ANCHORED_SHADOW"],1)
        match=report["shadow_paired_comparisons"][0]
        self.assertEqual(match["paired_rounds"],1)
        self.assertEqual(match["shadow_mae_usd"],1.5)
        self.assertEqual(match["ai_mae_usd"],9.5)
        self.assertEqual(match["price_path_mae_usd"],.5)
        self.assertEqual(match["shadow_better_than_ai_count"],1)
        self.assertEqual(match["shadow_better_than_path_count"],0)


    def test_paired_direction_uses_same_officially_settled_round(self):
        snap={"settlement_forecast":{"current_reference_usd":100.0}}
        ai={"source":"AI_REVIEW","direction":"UP","forecast_final_mean_usd":102.0}
        path={"direction":"DOWN","forecast_final_mean_usd":99.0}
        rows=[
            ("HALFWAY",json.dumps(snap),json.dumps({"AI":ai,"PRICE_PATH":path}),101.0,"UP"),
            ("LATE",json.dumps(snap),json.dumps({"AI":ai,"PRICE_PATH":path}),99.0,"DOWN"),
            ("HALFWAY",json.dumps(snap),json.dumps({"AI":{**ai,"source":"INDICATORS_FALLBACK"},"PRICE_PATH":path}),99.0,"DOWN"),
            ("HALFWAY",json.dumps(snap),json.dumps({"AI":ai,"PRICE_PATH":path}),101.0,None),
        ]
        with patch.object(app,"iter_query_rows",return_value=rows):
            report=app.numeric_price_forecast_report()
        groups={x["checkpoint"]:x for x in report["paired_direction_comparisons"]}
        self.assertEqual(groups["HALFWAY"]["paired_rounds"],1)
        self.assertEqual(groups["HALFWAY"]["ai_only_correct"],1)
        self.assertEqual(groups["HALFWAY"]["path_wins"],0)
        self.assertEqual(groups["LATE"]["paired_rounds"],1)
        self.assertEqual(groups["LATE"]["path_only_correct"],1)


if __name__=="__main__":
    unittest.main()
