"""Price forecast must project the FUTURE final-minute BRTI mean, not copy current side."""
import unittest
from settlement_forecast import forecast_settlement, VERSION
from ai_confirmation import final_price_direction


def context(movement=0.0, seconds_left=450, target=100.0):
    end = 900
    now = end - seconds_left
    points = [(t, 100.0 + movement*t) for t in range(int(now)+1)]
    return ({"status": "fresh", "window_end": end, "points": points},
            {"momentum_pct": round(movement*180/100*100, 4), "candle_age_seconds": 15,
             "pressure_available": False, "external_intelligence": {}},
            target, seconds_left, now)


class FutureSettlementTests(unittest.TestCase):
    def test_rising_future_can_cross_when_current_below(self):
        ref, ext, target, left, now = context(movement=.003, target=101.37)
        f = forecast_settlement(ref,ext,target,left,now=now)
        self.assertEqual(f["status"],"available")
        self.assertEqual(f["quantity"],"future_final_60_second_BRTI_average")
        self.assertLess(f["current_reference_usd"], target)
        self.assertGreater(f["forecast_final_mean_usd"], target)
        self.assertEqual(f["projected_direction"],"UP")
        self.assertGreater(f["forecast_delta_from_current_usd"],0)

    def test_falling_future_can_cross_when_current_above(self):
        ref,ext,target,left,now=context(movement=-.003,target=98.63)
        f=forecast_settlement(ref,ext,target,left,now=now)
        self.assertEqual(f["status"],"available")
        self.assertGreater(f["current_reference_usd"],target)
        self.assertLess(f["forecast_final_mean_usd"],target)
        self.assertEqual(f["projected_direction"],"DOWN")

    def test_future_information_cannot_leak(self):
        ref,ext,target,left,now=context(movement=.003,target=101.37)
        a=forecast_settlement(ref,ext,target,left,now=now)
        ref["points"] += [(t,300.0) for t in range(int(now)+1,901)]
        b=forecast_settlement(ref,ext,target,left,now=now)
        self.assertEqual(a["forecast_final_mean_usd"], b["forecast_final_mean_usd"])
        self.assertEqual(a["projected_direction"],b["projected_direction"])

    def test_no_brtI_means_no_forecast_from_spot(self):
        ref,ext,target,left,now=context(target=100)
        ref["status"]="unavailable"
        f=forecast_settlement(ref,ext,target,left,now=now)
        self.assertEqual(f["status"],"unavailable")
        self.assertNotIn("forecast_final_mean_usd",f)

    def test_verified_final_window_combines_known_and_future(self):
        ref,ext,target,left,now=context(movement=0.0, seconds_left=30, target=99.9)
        f=forecast_settlement(ref,ext,target,left,now=now)
        self.assertEqual(f["status"],"available")
        self.assertEqual(f["observed_final_minute_seconds"],31)
        self.assertEqual(f["modeled_final_minute_seconds"],29)
        self.assertAlmostEqual(f["forecast_final_mean_usd"],100.0,places=2)

    def test_price_itself_decides_AI_direction(self):
        ref,ext,target,left,now=context(movement=.003,target=101.37)
        ext["settlement_forecast"]=forecast_settlement(ref,ext,target,left,now=now)
        f,side=final_price_direction({"forecast_final_mean_usd":101.39},ext,target)
        self.assertEqual((f,side),(101.39,"UP"))
        f,side=final_price_direction({"forecast_final_mean_usd":101.34},ext,target)
        self.assertEqual((f,side),(101.34,"DOWN"))


if __name__=="__main__":
    unittest.main()
