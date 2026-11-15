"""Causal five-minute selloff/rebound detection; no trade or settlement override."""
import unittest

from market_shock import measure_brti_rapid_move, measure_market_shock

SOURCE = "Polymarket US BTC reference / BRTI"
NOW = 1000


def index_points(rebound=False):
    points = []
    for t in range(700, NOW + 1):
        if rebound:
            price = 100000 - 600 * (min(t, 940) - 700) / 240
            if t > 940:
                price += 80 * (t - 940) / 60
        else:
            price = 100000 - 550 * (t - 700) / 300
        points.append((t, price))
    return {"status": "fresh", "source": SOURCE, "points": points}


class RapidMoveTests(unittest.TestCase):
    def test_live_five_minute_selloff_without_completed_candle_delay(self):
        result = measure_brti_rapid_move(index_points(), now_epoch=NOW)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertEqual(result["change_usd"], -550)
        self.assertEqual(result["phase"], "continuing_down")
        self.assertEqual(result["lookback_seconds"], 300)
        self.assertFalse(result["predictive"])

    def test_rebound_not_mistaken_for_continuing_crash(self):
        result = measure_brti_rapid_move(index_points(rebound=True), now_epoch=NOW)
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertEqual(result["change_usd"], -520)
        self.assertEqual(result["phase"], "rebounding")
        self.assertGreater(result["change_60s_usd"], 0)

    def test_sparse_window_is_not_backfilled(self):
        ref = index_points()
        ref["points"] = [p for p in ref["points"] if p[0] < 810 or p[0] > 860]
        result = measure_brti_rapid_move(ref, now_epoch=NOW)
        self.assertEqual(result["status"], "unavailable")

    def test_stale_or_wrong_source_is_unavailable(self):
        ref = index_points()
        self.assertEqual(measure_brti_rapid_move(ref, now_epoch=NOW+30)["status"], "unavailable")
        ref["source"] = "Coinbase Exchange BTC-USD"
        self.assertEqual(measure_brti_rapid_move(ref, now_epoch=NOW)["status"], "unavailable")
        ref["source"] = SOURCE
        ref["status"] = "stale"
        self.assertEqual(measure_brti_rapid_move(ref, now_epoch=NOW)["status"], "unavailable")

    def test_malformed_future_and_duplicate_ticks_do_not_create_a_move(self):
        ref = index_points()
        ref["points"].extend([None, ("bad", 100), (999.5, 1), (1001, 1), (999, 99451.8333333333)])
        result = measure_brti_rapid_move(ref, now_epoch=NOW)
        self.assertEqual(result["kind"], "rapid_selloff")
        self.assertEqual(result["change_usd"], -550)

    def test_normal_five_minute_move_is_not_overclassified(self):
        ref = index_points()
        ref["points"] = [(t, 100000 - 100 * (t-700) / 300) for t, _ in ref["points"]]
        result = measure_brti_rapid_move(ref, now_epoch=NOW)
        self.assertEqual(result["kind"], "normal")

    def test_large_move_detected_even_if_baseline_already_volatile(self):
        start = 1700000040
        bars = []
        price = 100000.0
        for i in range(36):
            price += 200 if i < 31 else -100
            bars.append([start+60*i, price-210, price+210, price-210, price, 12])
        result = measure_market_shock(bars, now_epoch=start+36*60+10)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["net_5m_change_usd"], -500)
        self.assertTrue(result["rapid_move"])
        self.assertEqual(result["level"], "shock")
        self.assertEqual(result["direction"], "DOWN")


if __name__ == "__main__":
    unittest.main()
