"""Chronological holdout training must wait for verified complete BRTI labels."""
import json
import unittest
from forward_training import make_example, training_readiness, validate_cohort


def fixture(i, version="v2", source="Polymarket US BTC reference / BRTI", published=True):
    current=100+i*.001
    actual=current+(i%13-6)*.04
    winner="UP" if actual>=100 else "DOWN"
    snapshot={
        "version": version, "seconds_left":450, "official_target":100,
        "reference_price":{"status":"fresh","source":source,"price":current},
        "settlement_forecast":{"forecast_final_mean_usd":current+.04,
                               "net_trend_usd_per_min":.003,"current_reference_usd":current},
        "chart_pattern":{"momentum_3m_pct":float(i%11-5)*.005}
    }
    calls={"PRICE_PATH":{"direction":"UP","forecast_final_mean_usd":current+.04}}
    return (f"m-{i}","HALFWAY",f"2026-10-{i//31+1:02d}T{i%24:02d}:00:00Z",
            json.dumps(snapshot),json.dumps(calls),winner,actual if published else None)


class ChronologicalLabTests(unittest.TestCase):
    def test_rejects_missing_official_index_or_numeric_outcome(self):
        self.assertIsNone(make_example(fixture(0,published=False)))
        self.assertIsNone(make_example(fixture(1,source="Coinbase spot")))
        self.assertIsNotNone(make_example(fixture(2)))

    def test_small_sample_never_trains(self):
        report=training_readiness([fixture(i) for i in range(60)])
        self.assertEqual(report["cohorts"][0]["status"],"collecting")
        self.assertEqual(report["cohorts"][0]["shortfall"],240)
        self.assertFalse(report["auto_promote"])

    def test_walkforward_trains_only_when_300_labels_exist(self):
        report=training_readiness([fixture(i) for i in range(320)])
        g=report["cohorts"][0]
        self.assertEqual(g["graded_complete_rounds"],320)
        self.assertEqual(g["evaluation"]["train_rounds"],224)
        self.assertEqual(g["evaluation"]["holdout_rounds"],96)
        self.assertTrue(g["live_model_unchanged"])
        self.assertIn(g["status"],("validated_candidate","no_verified_improvement"))

    def test_separate_versions_and_checkpoints(self):
        rows=[fixture(i,version="v1") for i in range(170)]
        rows += [fixture(i+171,version="v2") for i in range(170)]
        report=training_readiness(rows)
        self.assertEqual(len(report["cohorts"]),2)
        self.assertTrue(all(g["status"]=="collecting" for g in report["cohorts"]))


if __name__=="__main__":unittest.main()
