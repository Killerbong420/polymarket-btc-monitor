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

    def test_bots_not_synonymous_with_ai(self):
        self.assertEqual(consensus({"AI":{"direction":"UP","source":"AI_REVIEW"}})["direction"],"WAIT")
        self.assertEqual(consensus({"AI":{"direction":"UP","source":"INDICATORS_FALLBACK"},
            "BOT_MACD":{"direction":"UP"},"PRICE_ACTION":{"direction":"UP"}})["direction"],"WAIT")
        calls={"AI":{"direction":"UP","source":"AI_REVIEW"},
               "BOT_MACD":{"direction":"UP"},"PRICE_ACTION":{"direction":"UP"}}
        self.assertEqual(consensus(calls)["direction"],"UP")
        calls["BOT_BOLLINGER"]={"direction":"DOWN"}
        self.assertEqual(consensus(calls)["direction"],"WAIT")
        calls["BOT_BOLLINGER"]["direction"]="WAIT"
        self.assertEqual(consensus(calls)["direction"],"UP")

    def test_context_only_and_source_provenance(self):
        p=analyze_price_action(candles())
        self.assertEqual(p["status"],"ready")
        self.assertIn(p["direction"], ("UP","DOWN","WAIT"))
        self.assertIn("source",p)


if __name__ == "__main__":
    unittest.main()
