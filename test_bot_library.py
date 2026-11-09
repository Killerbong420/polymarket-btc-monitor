"""No-hindsight public bot studies and forward prediction audit regression tests."""
import unittest
from bot_library import study_strategies, consensus, STRATEGIES
from price_action import analyze_price_action


def candles(mode="trend", count=80):
    arr = []
    price = 80000.0
    for i in range(count):
        delta = (2.0 + i * 0.11) if mode == "trend" else (8 if i % 2 else -8)
        op = price
        price += delta
        arr.append([1785000000 + 60*i, min(op,price)-2.0,
                    max(op,price)+2.0, op, price, 100 + i])
    return arr


class LibraryTests(unittest.TestCase):
    def test_three_named_strategy_results(self):
        result=study_strategies(candles())
        self.assertEqual(set(result), set(STRATEGIES))
        for name,call in result.items():
            self.assertIn(call["direction"], ("UP","DOWN","WAIT"))
            self.assertEqual(call["confidence"], "UNRATED")
            self.assertIn("model_version", call)

    def test_rejects_short_gapped_and_bad_candles(self):
        self.assertTrue(all(call["direction"]=="WAIT" for call in study_strategies(candles()[:40]).values()))
        bars=candles()
        bars[27][0]+=120
        self.assertTrue(all(call["direction"]=="WAIT" for call in study_strategies(bars).values()))
        bars=candles()
        bars[-1][4]=-12
        self.assertTrue(all(call["direction"]=="WAIT" for call in study_strategies(bars).values()))

    def test_no_future_bar_information(self):
        bars=candles()
        early=study_strategies(bars)
        with_future=candles(count=81)
        with_future[-1][4]+=100000
        # The earlier snapshot must yield identical decisions when replayed.
        self.assertEqual(early, study_strategies(with_future[:-1]))

    def test_no_ai_required_for_research_consensus(self):
        from bot_library import VERSION, CONSENSUS_VERSION
        self.assertEqual(consensus({"AI": {"direction": "UP", "source": "AI_REVIEW"}})["direction"], "WAIT")
        valid = {"BOT_MACD": {"direction": "UP", "source": VERSION},
                 "PRICE_ACTION": {"direction": "UP", "source": "PRICE_ACTION_V1"}}
        baseline = consensus(valid)
        self.assertEqual(baseline["direction"], "UP")
        self.assertEqual(baseline["source"], CONSENSUS_VERSION)
        self.assertFalse(baseline["calibrated_probability"])
        self.assertTrue(baseline["research_only"])
        self.assertEqual(baseline, consensus({**valid, "AI": {"direction": "DOWN", "source": "AI_REVIEW"}}))
        self.assertEqual(baseline, consensus({**valid, "AI": {"direction": "UP", "source": "INDICATORS_FALLBACK"}}))

    def test_conflicting_stale_and_correlated_bots_abstain(self):
        from bot_library import VERSION
        trend = {"BOT_MACD": {"direction": "UP", "source": VERSION},
                 "BOT_SUPERTREND": {"direction": "UP", "source": VERSION}}
        correlated = consensus(trend)
        self.assertEqual(correlated["direction"], "WAIT")
        self.assertIn("overlapping", correlated["reason"])
        self.assertEqual(consensus({**trend, "PRICE_ACTION": {"direction": "DOWN", "source": "PRICE_ACTION_V1"}})["direction"], "WAIT")
        self.assertEqual(consensus({**trend, "PRICE_ACTION": {"direction": "UP", "source": "PRICE_ACTION_ABSTAIN"}})["direction"], "WAIT")
        self.assertEqual(consensus({"BOT_MACD": {"direction": "UP", "source": "BOT_STALE"},
                                    "PRICE_ACTION": {"direction": "UP", "source": "PRICE_ACTION_V1"}})["direction"], "WAIT")
        confirmed = consensus({**trend, "PRICE_ACTION": {"direction": "UP", "source": "PRICE_ACTION_V1"}})
        self.assertEqual(confirmed["direction"], "UP")
        self.assertEqual(confirmed["eligible_votes"], 3)
        self.assertEqual(consensus({"BOT_BOLLINGER": {"direction": "DOWN", "source": VERSION},
                                    "PRICE_ACTION": {"direction": "DOWN", "source": "PRICE_ACTION_V1"}})["direction"], "DOWN")
        self.assertEqual(consensus(None)["direction"], "WAIT")

    def test_context_only_and_source_provenance(self):
        p=analyze_price_action(candles())
        self.assertEqual(p["status"],"ready")
        self.assertIn(p["direction"], ("UP","DOWN","WAIT"))
        self.assertIn("source",p)


if __name__ == "__main__":
    unittest.main()
