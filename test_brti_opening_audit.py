import unittest
from forecast_guard import validate_official_target


class OpeningAuditTests(unittest.TestCase):
    def test_preboundary_only_and_official_authority(self):
        start = 1800
        ref = {
            "window_start": start,
            "opening_average": 100.0,
            "opening_average_samples": 60,
            "points": [[t, 100.0] for t in range(start - 60, start)]
                      + [[start, 999999.0], [start + 1, 999999.0]],
        }
        result = validate_official_target(120.0, ref)
        self.assertEqual(result["official_target_usd"], 120.0)
        self.assertTrue(result["official_target_authoritative"])
        self.assertTrue(result["material"])
        audit = result["opening_sample_audit"]
        self.assertEqual(audit["observed_seconds"], 60)
        self.assertEqual(audit["missing_seconds"], [])
        self.assertEqual(audit["first_epoch"], start - 60)
        self.assertEqual(audit["last_epoch"], start - 1)
        self.assertEqual(audit["mean_close_usd"], 100.0)

    def test_missing_seconds_preserve_guard(self):
        result = validate_official_target(100.0, {
            "window_start": 1800,
            "opening_average": None,
            "opening_average_samples": 59,
            "points": [[t, 100] for t in range(1740, 1800) if t != 1750],
        })
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["opening_sample_audit"]["missing_seconds"], [1750])

    def test_invalid_and_future_samples_ignored(self):
        result = validate_official_target(100, {
            "window_start": 1800,
            "opening_average": 100,
            "opening_average_samples": 60,
            "points": [[1740, 100], [1799, 101], [1800, 900],
                       ["bad", 42], [1798, float("nan")], None],
        })
        self.assertEqual(result["opening_sample_audit"]["observed_seconds"], 2)
        self.assertEqual(result["opening_sample_audit"]["maximum_close_usd"], 101)


if __name__ == "__main__":
    unittest.main()
