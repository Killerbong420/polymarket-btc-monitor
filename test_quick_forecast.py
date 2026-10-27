"""Causal, 60-second BRTI quick-call regression tests."""
import unittest
from quick_forecast import quick_forecast, INDEX_SOURCE


def reference(now=1000, slope=.40):
    return {"status": "fresh", "source": INDEX_SOURCE,
            "points": [(t, 80000+slope*(t-700)) for t in range(700,now+1)]}


class QuickForecastTests(unittest.TestCase):
    def test_rising_brtI_projects_future_up(self):
        r=quick_forecast(reference(),now=1000)
        self.assertEqual(r["status"],"available")
        self.assertEqual(r["direction"],"UP")
        self.assertGreater(r["forecast_price_60s_usd"],r["current_reference_usd"])
        self.assertEqual(r["horizon_seconds"],60)
        self.assertFalse(r["calibrated_probability"])
        self.assertTrue(all(x<=120 for x in r["input_spans_seconds"]))

    def test_falling_brtI_projects_future_down(self):
        r=quick_forecast(reference(slope=-.55),now=1000)
        self.assertEqual(r["direction"],"DOWN")
        self.assertLess(r["forecast_move_usd"],0)

    def test_reversal_warning_from_opposing_slopes(self):
        r=reference(slope=1.5)
        updated=[]
        for t,p in r["points"]:
            if t>988:
                p=(80000+1.5*(988-700))-1.5*(t-988)
            updated.append((t,p))
        r["points"]=updated
        f=quick_forecast(r,now=1000)
        self.assertEqual(f["status"],"available")
        self.assertTrue(f["reversal_watch"])
        self.assertEqual(f["reversal_direction"],"DOWN")

    def test_future_observations_do_not_leak_into_now(self):
        r=reference()
        original=quick_forecast(r,now=1000)
        r["points"] += [(t, 300000) for t in range(1001,1200)]
        self.assertEqual(original,quick_forecast(r,now=1000))

    def test_no_index_or_stale_ticks_cannot_be_a_fast_call(self):
        r=reference()
        r["status"]="unavailable"
        self.assertEqual(quick_forecast(r,now=1000)["status"],"unavailable")
        r=reference()
        r["source"]="Coinbase BTC-USD"
        self.assertEqual(quick_forecast(r,now=1000)["status"],"unavailable")
        r=reference(now=980)
        self.assertEqual(quick_forecast(r,now=1000)["status"],"unavailable")

    def test_sparse_history_and_bad_points_not_invented(self):
        r=reference()
        r["points"]=[(997,80000),(998,80001),(999,80002),(1000,80003)]
        self.assertEqual(quick_forecast(r,now=1000)["status"],"unavailable")
        r=reference()
        r["points"]=[("bad",80000),(-10,2),None,*r["points"]]
        self.assertEqual(quick_forecast(r,now=1000)["direction"],"UP")

    def test_flat_conditions_do_not_invent_edge(self):
        r=reference(slope=0)
        f=quick_forecast(r,now=1000)
        self.assertEqual(f["status"],"available")
        self.assertEqual(f["direction"],"WAIT")
        self.assertEqual(f["quality"],"NO_EDGE")


if __name__=="__main__":
    unittest.main()
