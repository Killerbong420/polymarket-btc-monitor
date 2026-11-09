"""TerryCalls stays a read-only, same-market 15m signal and graded shadow source."""
import os
import time
import unittest
from unittest.mock import patch

import terry_calls as terry


class TerryCallsTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.market = "cpc-btc-updown-15m-2026-11-09-1515z"
        from datetime import datetime, timezone, timedelta
        fmt = lambda n: datetime.fromtimestamp(n, timezone.utc).isoformat().replace("+00:00", "Z")
        self.data = {"updated_at": fmt(self.now - 2), "stale": False, "markets": [{
            "market": self.market, "horizon": "15m", "window_end": fmt(self.now+400),
            "call": {"side": "up", "at": fmt(self.now-40), "chance": .72, "price": .45},
            "chance_now": {"up": .61, "down": .39}
        }]}
        self.local = {"market": self.market, "status": "running", "updated": fmt(self.now-1),
                      "guarded_primary": {"status": "available", "direction": "UP"}}
        terry._CACHE.update(payload=self.data, received=self.now-1, error=None)

    @patch.dict(os.environ, {"TERRY_API_KEY": "dummy-secret"}, clear=False)
    def test_matches_verified_current_market_only(self):
        s = terry.snapshot(self.local, self.now)
        self.assertEqual(s["status"], "live")
        self.assertEqual(s["terry_call"]["side"], "UP")
        self.assertEqual(s["terry_call"]["chance_pct"], 72.0)
        self.assertEqual(s["terry_chance_now"], {"up_pct": 61.0, "down_pct": 39.0})
        self.assertEqual(s["our_call"]["side"], "UP")
        self.assertEqual(s["comparison"], "AGREE")
        self.assertNotIn("dummy-secret", str(s))

    @patch.dict(os.environ, {"TERRY_API_KEY": "dummy-secret"}, clear=False)
    def test_rejects_wrong_round_and_stale_response(self):
        self.assertEqual(terry.snapshot({**self.local, "market": "different"}, self.now)["status"], "market_mismatch")
        self.data["stale"] = True
        self.assertEqual(terry.snapshot(self.local, self.now)["status"], "stale")
        self.data["stale"] = False
        self.assertEqual(terry.snapshot(self.local, self.now+38)["status"], "unavailable")

    @patch.dict(os.environ, {"TERRY_API_KEY": "dummy-secret"}, clear=False)
    def test_no_our_call_when_local_monitor_stale(self):
        s = terry.snapshot({**self.local, "status": "feed error"}, self.now)
        self.assertIsNone(s["our_call"])
        self.assertEqual(s["comparison"], "UNAVAILABLE")

    def test_missing_api_key_never_uses_cached_results(self):
        with patch.dict(os.environ, {"TERRY_API_KEY": ""}, clear=False):
            self.assertEqual(terry.snapshot(self.local, self.now)["status"], "not_configured")

    @patch.dict(os.environ, {"TERRY_API_KEY": "dummy-secret"}, clear=False)
    def test_invalid_probabilities_hidden(self):
        self.data["markets"][0]["chance_now"] = {"up": 92, "down": -1}
        self.data["markets"][0]["call"]["chance"] = "high"
        s = terry.snapshot(self.local, self.now)
        self.assertIsNone(s["terry_call"]["chance_pct"])
        self.assertEqual(s["terry_chance_now"], {"up_pct": None, "down_pct": None})

    def test_score_only_from_matching_provider_history(self):
        rows = [[self.market, "UP", "UP", "PENDING"]]
        saved = []
        def execute(sql, params=(), fetch=False):
            if "UPDATE terry_15m_comparisons" in sql:
                saved.append(params)
                rows[0][3] = params[0]
            return [tuple(x) for x in rows] if fetch else None
        report = terry._grade_and_summarize(execute, [{"market": self.market, "horizon": "15m",
                                  "result": "down", "call": {"side": "up"}}])
        self.assertEqual(saved, [("DOWN", self.market)])
        self.assertEqual(report["settled"], 1)
        self.assertEqual(report["ours_right"], 0)
        self.assertEqual(report["terry_right"], 0)

    def test_provider_history_different_call_cannot_grade_pair(self):
        rows = [(self.market, "UP", "UP", "PENDING")]
        def execute(sql, params=(), fetch=False):
            if "UPDATE" in sql:
                self.fail("Mismatch must not grade")
            return rows if fetch else None
        report = terry._grade_and_summarize(execute, [{"market": self.market, "horizon": "15m",
                                "result": "down", "call": {"side": "down"}}])
        self.assertEqual(report["settled"], 0)


if __name__ == "__main__":
    unittest.main()
