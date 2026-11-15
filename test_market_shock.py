"""Regression checks for the observed, non-predictive market-shock card."""
import unittest

from market_shock import measure_market_shock, observe_flash_move


START = 1_700_000_040  # exact minute boundary


def bars(volumes=None, moves=None):
    result = []
    price = 50_000.0
    for n in range(36):
        price += moves[n] if moves is not None else 10.0
        volume = volumes[n] if volumes is not None else 12.0
        result.append([START + 60*n, price - 20, price + 20, price - 10, price, volume])
    return result


NOW = START + 36*60 + 10


class MarketShockTests(unittest.TestCase):
    def test_normal_does_not_claim_shock(self):
        got = measure_market_shock(bars(), "Coinbase BTC-USD", now_epoch=NOW)
        self.assertEqual(got["status"], "available")
        self.assertEqual(got["level"], "normal")
        self.assertEqual(got["volume_multiple"], 1.0)
        self.assertGreater(got["movement_multiple"], 0.9)

    def test_high_volume_captures_market_shock(self):
        volumes = [12.0]*31 + [48.0]*5
        got = measure_market_shock(bars(volumes=volumes), now_epoch=NOW)
        self.assertEqual(got["level"], "shock")
        self.assertEqual(got["volume_multiple"], 4.0)
        self.assertEqual(got["direction"], "UP")

    def test_high_movement_without_volume_spike(self):
        moves = [10.0]*31 + [-100.0]*5
        got = measure_market_shock(bars(moves=moves), now_epoch=NOW)
        self.assertEqual(got["level"], "shock")
        self.assertEqual(got["volume_multiple"], 1.0)
        self.assertGreater(got["movement_multiple"], 5.0)
        self.assertEqual(got["direction"], "DOWN")

    def test_elevated_is_between_normal_and_shock(self):
        volumes = [12.0]*31 + [24.0]*5
        got = measure_market_shock(bars(volumes=volumes), now_epoch=NOW)
        self.assertEqual(got["level"], "elevated")
        self.assertEqual(got["volume_multiple"], 2.0)

    def test_missing_minutes_unavailable_not_fake_shock(self):
        sample = bars()
        sample[-10][0] += 30
        got = measure_market_shock(sample, now_epoch=NOW)
        self.assertEqual(got["status"], "unavailable")
        self.assertIsNone(got["volume_multiple"])

    def test_stale_exchange_data_unavailable(self):
        got = measure_market_shock(bars(), now_epoch=NOW+240)
        self.assertEqual(got["level"], "unavailable")

    def test_insufficient_history_unavailable(self):
        got = measure_market_shock(bars()[-10:], now_epoch=NOW)
        self.assertEqual(got["status"], "unavailable")

    def test_no_volume_baseline_unavailable(self):
        got = measure_market_shock(bars(volumes=[0.0]*36), now_epoch=NOW)
        self.assertEqual(got["level"], "unavailable")


class FlashMoveTests(unittest.TestCase):
    SOURCE = "Polymarket US BTC reference / BRTI"

    @staticmethod
    def reference(prices, start=1_700_000_000, remove=()):
        return {
            "status": "fresh", "source": FlashMoveTests.SOURCE,
            "points": [(start + i, price) for i, price in enumerate(prices) if i not in remove],
        }

    def test_detects_500_selloff_without_waiting_for_completed_candles(self):
        prices = [65000 - 2*i for i in range(330)]
        r = self.reference(prices)
        alert = observe_flash_move(r, 1_700_000_329)
        self.assertEqual(alert["status"], "available")
        self.assertEqual(alert["level"], "flash_selloff")
        self.assertEqual(alert["direction"], "DOWN")
        self.assertGreaterEqual(alert["five_minute_drawdown_usd"], 500)
        self.assertEqual(alert["source"], self.SOURCE)

    def test_warns_on_one_minute_drop_before_five_minute_threshold(self):
        prices = [64000]*260 + [64000 - 5*i for i in range(61)]
        alert = observe_flash_move(self.reference(prices), 1_700_000_320)
        self.assertEqual(alert["level"], "rapid_selloff")
        self.assertEqual(alert["last_60s_change_usd"], -300.0)
        self.assertLess(alert["five_minute_drawdown_usd"], 500)

    def test_detects_rally_without_mislabelling_as_selling(self):
        prices = [64000 + 2*i for i in range(330)]
        alert = observe_flash_move(self.reference(prices), 1_700_000_329)
        self.assertEqual(alert["level"], "flash_rally")
        self.assertEqual(alert["direction"], "UP")

    def test_old_selloff_then_newer_rebound_does_not_claim_current_downtrend(self):
        prices = [65000 - 5*i for i in range(120)] + [64405 + 6*i for i in range(110)]
        alert = observe_flash_move(self.reference(prices), 1_700_000_229)
        self.assertEqual(alert["level"], "flash_rally")

    def test_normal_small_changes(self):
        prices = [64000 + .1*i for i in range(320)]
        alert = observe_flash_move(self.reference(prices), 1_700_000_319)
        self.assertEqual(alert["level"], "normal")
        self.assertIsNone(alert["direction"])

    def test_stale_or_wrong_source_is_unavailable(self):
        r = self.reference([64000 - 5*i for i in range(120)])
        self.assertEqual(observe_flash_move(r, 1_700_000_200)["level"], "unavailable")
        r["source"] = "Coinbase BTC-USD"
        self.assertEqual(observe_flash_move(r, 1_700_000_119)["level"], "unavailable")

    def test_missing_ticks_does_not_fabricate_big_move(self):
        prices = [65000 - 5*i for i in range(320)]
        r = self.reference(prices, remove=set(range(95, 180)))
        alert = observe_flash_move(r, 1_700_000_319)
        self.assertEqual(alert["status"], "unavailable")

    def test_too_few_ticks_is_unavailable(self):
        prices = [64000 - 10*i for i in range(20)]
        self.assertEqual(observe_flash_move(self.reference(prices), 1_700_000_019)["status"], "unavailable")

    def test_does_not_depend_on_official_target_validation(self):
        r = self.reference([65000 - 2*i for i in range(330)])
        r["opening_average"] = 64500
        r["opening_average_samples"] = 60
        self.assertEqual(observe_flash_move(r, 1_700_000_329)["level"], "flash_selloff")


if __name__ == "__main__":
    unittest.main()
