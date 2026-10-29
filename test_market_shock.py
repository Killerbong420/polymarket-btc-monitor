"""Regression checks for the observed, non-predictive market-shock card."""
import unittest

from market_shock import measure_market_shock


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


if __name__ == "__main__":
    unittest.main()
