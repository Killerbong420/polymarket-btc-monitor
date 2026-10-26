"""Regression checks for pre-settlement selective confluence signals."""
import unittest
from research import confluence_forecast

class ConfluenceTests(unittest.TestCase):
    def setUp(self):
        self.ai = {"direction":"UP","source":"AI_REVIEW","confidence":"MEDIUM"}
        self.behavior = {
            "status":"available","is_reference":True,"side":"UP",
            "target_gap_pct":.12,"price_age_seconds":2,"coverage_pct":96,
            "current_side_run_seconds":180,"observed_crossings":1,
            "lead_behavior":"GROWING","near_target":False}
        self.ref = {"status":"fresh"}
        self.chart = {"atr14_pct":.08}

    def decide(self):
        return confluence_forecast(self.ai,self.behavior,self.ref,self.chart)

    def test_confirmed_index_ai_agreement(self):
        self.assertEqual(self.decide()["direction"],"UP")

    def test_ai_disagreement_abstains(self):
        self.ai["direction"]="DOWN"
        self.assertEqual(self.decide()["direction"],"WAIT")

    def test_low_confidence_abstains(self):
        self.ai["confidence"]="LOW"
        self.assertEqual(self.decide()["direction"],"WAIT")

    def test_stale_or_spot_only_reference_abstains(self):
        self.ref["status"]="stale"
        self.assertEqual(self.decide()["direction"],"WAIT")
        self.ref["status"]="fresh"
        self.behavior["is_reference"]=False
        self.assertEqual(self.decide()["direction"],"WAIT")

    def test_small_or_shrinking_lead_abstains(self):
        self.behavior["target_gap_pct"]=.02
        self.assertEqual(self.decide()["direction"],"WAIT")
        self.behavior["target_gap_pct"]=.12
        self.behavior["lead_behavior"]="SHRINKING"
        self.assertEqual(self.decide()["direction"],"WAIT")

    def test_repeated_crossings_abstains(self):
        self.behavior["observed_crossings"]=4
        self.assertEqual(self.decide()["direction"],"WAIT")

    def test_low_coverage_abstains(self):
        self.behavior["coverage_pct"]=25
        self.assertEqual(self.decide()["direction"],"WAIT")

if __name__ == "__main__":
    unittest.main()
