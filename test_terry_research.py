"""Regression tests for read-only TerryCalls historical and prospective research."""
import unittest
import terry_research as t


def item(market, side="up", result="up", chance=.8, price=.7,
         seconds_in=400, rule="v1", lock=None):
    return {"market": f"cpc-btc-updown-15m-2026-11-09-{market}z",
            "horizon": "15m", "result": result,
            "call": {"side": side, "chance": chance, "price": price,
                     "seconds_in": seconds_in, "rule": rule}, "lock": lock}


class TerryStudyTests(unittest.TestCase):
    def test_deduplicates_settled_provider_markets_only(self):
        valid = item("1500")
        report = t.summarize_history([valid, valid, item("1515", result=None),
                                      {**item("1530"), "horizon": "5m"}])
        self.assertEqual(report["settled_windows"], 1)
        self.assertEqual(report["calls"]["accuracy_pct"], 100)
        self.assertEqual(report["calls"]["settled"], 1)

    def test_probability_and_entry_quote_are_not_equated(self):
        report = t.summarize_history([item("1500", chance=.8, price=.9, result="down"),
                                      item("1515", chance=.6, price=.4, result="up")])
        self.assertEqual(report["calls"]["right"], 1)
        self.assertEqual(report["calls"]["accuracy_pct"], 50.)
        self.assertAlmostEqual(report["calls"]["brier_score"], .40)
        self.assertLess(report["calls"]["hypothetical_taker_net_one_contract_each_usd"], 0.0)
        self.assertEqual(report["calls"]["quoted_entry_samples"], 2)

    def test_separates_immutable_calls_locks_and_rules(self):
        lock = {"side": "down", "chance": .95, "price": .9, "rule": "lock-v1"}
        report = t.summarize_history([
            item("1500", result="down", rule="old", lock=lock),
            item("1515", result="up", rule="new")])
        self.assertEqual(report["locks_separate"]["settled"], 1)
        self.assertEqual(report["locks_separate"]["right"], 1)
        self.assertEqual({r["group"] for r in report["calls_by_rule"]}, {"old", "new"})

    def test_rejects_corrupt_input_and_never_false_certainty(self):
        report = t.summarize_history([
            item("1500", chance=float("nan"), price=3),
            item("1515", chance=True, price=True),
            {"market": "bad", "result": "up", "horizon": "15m", "call": {"side": "up"}}])
        self.assertEqual(report["called_windows"], 2)
        self.assertIsNone(report["calls"]["brier_score"])
        self.assertEqual(report["calls"]["quoted_entry_samples"], 0)
        self.assertIn("not fills", report["limitations"])

    def test_uncalled_window_counts_coverage_not_accuracy(self):
        uncalled = item("1515")
        uncalled["call"] = None
        report = t.summarize_history([item("1500"), uncalled])
        self.assertEqual(report["settled_windows"], 2)
        self.assertEqual(report["called_windows"], 1)
        self.assertEqual(report["call_coverage_pct"], 50.)

    def test_forward_scores_same_market_same_minute_and_not_overcount_duplicate(self):
        points = [("m1", 7, .8, "UP", "UP"), ("m1", 7, .1, "DOWN", "UP"),
                  ("m2", 7, .7, "DOWN", "DOWN"), ("m3", 8, .5, "UP", "UP")]
        report = t.summarize_forward(points)
        self.assertEqual(report["status"], "observed")
        cp = report["checkpoints"][0]
        self.assertEqual(cp["market_minute"], 8)
        self.assertEqual(cp["settled_samples"], 2)
        self.assertEqual(cp["terry_direction_accuracy_pct"], 50.)
        self.assertEqual(cp["terry_paired_accuracy_pct"], 50.)
        self.assertEqual(cp["ours_paired_accuracy_pct"], 100.)
        self.assertEqual(report["checkpoints"][1]["paired_rounds"], 0)

    def test_forward_rejects_invalid_probabilities_and_results(self):
        report = t.summarize_forward([
            ("m1", 0, 1.5, "UP", "UP"), ("m2", 3, .6, "UP", None),
            ("m3", 15, .8, "UP", "UP"), ("m4", 2, True, "UP", "UP")])
        self.assertEqual(report["status"], "collecting")


if __name__ == "__main__":
    unittest.main()
