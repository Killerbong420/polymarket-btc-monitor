"""SQLite integration coverage for Terry's bounded archival shadow study."""
import json
import os
import sqlite3
import time
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import terry_calls as tc


def ts(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class TerryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(":memory:")
        def execute(sql, params=(), fetch=False):
            result = self.c.execute(sql, params)
            self.c.commit()
            return result.fetchall() if fetch else None
        self.execute = execute
        tc._setup_table(self.execute)
        # Anchor within a minute: now+4 must remain in the same bucket.
        # Wall-clock timing at :58/:59 previously made this test flaky.
        self.now = float((int(time.time()) // 60) * 60 + 10)
        self.market = "cpc-btc-updown-15m-2026-11-09-1500z"
        self.provider = {"market": self.market, "horizon": "15m",
                         "window_end": ts(self.now + 480),
                         "chance_now": {"up": .76, "down": .24}}
        self.sample = {"status": "live", "market": self.market,
                       "our_call": {"side": "DOWN"}}

    def tearDown(self):
        self.c.close()

    def test_live_shadow_written_once_no_reconstruction(self):
        with self.c:
            saved = tc._save_forward_sample(self.execute, self.sample, self.provider, self.now)
            self.assertEqual(saved[0], self.market)
            tc._save_forward_sample(self.execute, self.sample, self.provider, self.now + 4)
        rows = self.execute("SELECT * FROM terry_15m_shadow", fetch=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][-1], "DOWN")
        self.assertAlmostEqual(rows[0][-2], .76)

    def test_reject_mismatched_and_inconsistent_provider_data(self):
        self.provider["market"] = "cpc-btc-updown-15m-other"
        self.assertIsNone(tc._save_forward_sample(self.execute, self.sample, self.provider, self.now))
        self.provider["market"] = self.market
        self.provider["chance_now"] = {"up": .8, "down": .8}
        self.assertIsNone(tc._save_forward_sample(self.execute, self.sample, self.provider, self.now))
        self.assertEqual(self.execute("SELECT COUNT(*) FROM terry_15m_shadow", fetch=True)[0][0], 0)

    def test_persistent_history_scores_only_verified_provider_round(self):
        self.assertIsNotNone(tc._save_forward_sample(
            self.execute, self.sample, self.provider, self.now))
        history = [{"market": self.market, "horizon": "15m", "result": "up",
                    "call": {"side": "down", "chance": .76, "price": .72,
                             "seconds_in": 420, "rule": "v2"}}]
        self.assertEqual(tc._archive_history(self.execute, history), 1)
        self.assertEqual(tc._archive_history(self.execute, history), 0)
        research = tc._research_report(self.execute)
        self.assertEqual(research["historical_calls"]["called_windows"], 1)
        self.assertEqual(research["forward_minute_checkpoints"]["checkpoints"][0]["settled_samples"], 1)
        self.assertEqual(research["forward_minute_checkpoints"]["checkpoints"][0]["terry_paired_accuracy_pct"], 100.)
        self.assertEqual(research["forward_minute_checkpoints"]["checkpoints"][0]["ours_paired_accuracy_pct"], 0.)
        self.assertNotIn("TERRY_API_KEY", json.dumps(research))


if __name__ == "__main__":
    unittest.main()
