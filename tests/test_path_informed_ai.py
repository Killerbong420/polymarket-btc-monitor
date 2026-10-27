"""Guard that path context is fresh BRTI, official-target matched, and not a spot proxy."""
import unittest
from ai_confirmation import trusted_price_path

def evidence():
    return {
        "reference_price": {
            "status": "fresh", "source": "Polymarket US BTC reference / BRTI",
            "price": 80020.0, "age_seconds": 2
        },
        "settlement_forecast": {
            "status": "available", "quality": "OBSERVED", "version": "settlement-forecast",
            "forecast_final_mean_usd": 80010.0, "target_usd": 80000.0,
            "current_reference_usd": 80020.0, "age_seconds": 2,
            "downside_scenario_usd": 79980.0, "upside_scenario_usd": 80035.0,
            "opposing_short_long_slopes": False
        }
    }

class PathInputTests(unittest.TestCase):
    def test_valid_path_supplied(self):
        row=trusted_price_path(evidence(),80000)
        self.assertIsNotNone(row)
        self.assertEqual(row["projected_direction"],"UP")
        self.assertTrue(row["target_in_scenario_range"])

    def test_stale_reference_blocked(self):
        d=evidence()
        d["reference_price"]["age_seconds"]=25
        self.assertIsNone(trusted_price_path(d,80000))

    def test_wrong_target_blocked(self):
        self.assertIsNone(trusted_price_path(evidence(),80100))

    def test_spot_proxy_blocked(self):
        d=evidence()
        d["reference_price"]["source"]="Coinbase BTC-USD"
        self.assertIsNone(trusted_price_path(d,80000))

    def test_path_price_mismatch_blocked(self):
        d=evidence()
        d["settlement_forecast"]["current_reference_usd"]=81000
        self.assertIsNone(trusted_price_path(d,80000))

    def test_missing_baseline_never_invented(self):
        d=evidence()
        del d["settlement_forecast"]
        self.assertIsNone(trusted_price_path(d,80000))

if __name__=="__main__":
    unittest.main()
