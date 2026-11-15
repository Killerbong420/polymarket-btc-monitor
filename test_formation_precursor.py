"""Causal earliest-formation warning and fixed-horizon Coinbase outcome audit."""
import math
import sqlite3
import unittest
from pathlib import Path

from early_pattern_context import analyze_early_patterns
from formation_precursor import detect_precursor, precursor_stats, SOURCE
from pattern_lifecycle import setup_tables, record_completed_bar


def candles_with_impulse(sign=-1, *, pullback=0, thin=False):
    closes = [64000 + math.sin(i / 2) * 2 for i in range(35)]
    start = closes[-1]
    closes += [start + sign * 90 * i for i in range(1, 5)]
    if pullback:
        closes.append(closes[-1] - sign * 28)
    candles = []
    for i, close in enumerate(closes):
        previous = closes[i - 1] if i else close
        volume = 1.0 if thin and i >= 35 else (22.0 if i >= 35 else 10.0)
        candles.append([1800000000 + i * 60,
                        min(previous, close) - 1, max(previous, close) + 1,
                        previous, close, volume])
    return candles


class PrecursorTests(unittest.TestCase):
    def test_before_two_to_four_minute_flag_consolidation(self):
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                bars = candles_with_impulse(sign, pullback=1)
                original = [b.copy() for b in bars]
                result = analyze_early_patterns(bars, SOURCE)
                self.assertEqual(result["status"], "monitoring", result)
                p = result["precursor"]
                self.assertEqual(p["phase"], "FIRST_PULLBACK")
                self.assertEqual(p["hint"], "BEAR_FLAG" if sign == -1 else "BULL_FLAG")
                self.assertEqual(p["bias"], "DOWN" if sign == -1 else "UP")
                self.assertFalse(p["breakout_confirmed"])
                self.assertFalse(p["probability_calibrated"])
                self.assertEqual(p["research_horizons_minutes"], [5, 10, 15])
                self.assertEqual(bars, original)

    def test_impulse_detects_before_any_consolidation(self):
        for sign in (-1, 1):
            result = analyze_early_patterns(candles_with_impulse(sign), SOURCE)
            self.assertEqual(result["status"], "monitoring", result)
            self.assertEqual(result["precursor"]["phase"], "IMPULSE")
            self.assertEqual(result["precursor"]["bias"], "DOWN" if sign == -1 else "UP")

    def test_no_fake_signal_from_quiet_movement_or_invalid_data(self):
        bars = candles_with_impulse(-1, thin=True)
        self.assertNotIn("precursor", analyze_early_patterns(bars, SOURCE))
        flat = [[1800000000 + i * 60, 63998.0, 64002.0, 64000.0, 64000.0, 10.0]
                for i in range(40)]
        self.assertIsNone(detect_precursor(flat, atr=2.0))
        broken = candles_with_impulse()
        broken[-2][0] += 60
        self.assertEqual(analyze_early_patterns(broken, SOURCE)["status"], "unavailable")

    def test_confirmed_flag_takes_precedence_over_early_precursor(self):
        bars = candles_with_impulse(-1, pullback=1)
        for i in range(1, 3):
            close = bars[-1][4] + 30
            op = bars[-1][4]
            bars.append([bars[-1][0] + 60, min(op, close)-1, max(op, close)+1, op, close, 20])
        result = analyze_early_patterns(bars, SOURCE)
        self.assertEqual(result["pattern"], "BEAR_FLAG", result)
        self.assertEqual(result["status"], "watch")
        self.assertNotIn("precursor", result)


class ForwardOnlyStatsTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        setup_tables(self.execute)
        self.market = "cpc-btc-updown-15m-2026-11-14-1830z"
        self.start = 1800000060

    def tearDown(self):
        self.db.close()

    def execute(self, sql, params=(), fetch=False):
        cursor = self.db.execute(sql, params)
        rows = cursor.fetchall() if fetch else None
        self.db.commit()
        return rows

    def bar(self, epoch, close):
        return [epoch-60, close-1, close+1, close, close, 10]

    def signal(self, epoch, close, side="UP"):
        d = 1 if side == "UP" else -1
        return {"status": "monitoring", "source": SOURCE,
                "precursor": {
                    "status": "precursor", "phase": "IMPULSE",
                    "hint": "BULL_FLAG" if d > 0 else "BEAR_FLAG",
                    "bias": side, "anchor_epoch": epoch-60,
                    "last_completed_epoch": epoch, "reference_usd": close,
                    "trigger_usd": close + d*20,
                    "invalidation_usd": close - d*40,
                }}

    def record(self, epoch, close, signal=None, market=None):
        return record_completed_bar(self.execute,market or self.market,
             signal if signal is not None else {"status":"monitoring","source":SOURCE},
             self.bar(epoch,close),now=epoch+2)

    def test_fixed_horizons_scored_without_lookahead_or_calibration(self):
        first = self.record(self.start, 64000, self.signal(self.start,64000))
        self.assertEqual(first["precursor_horizons"]["horizons"][0]["samples"], 0)
        for m in range(1, 16):
            result = self.record(self.start + m*60, 64000 + m*4,
                 market="cpc-btc-updown-15m-next-round" if m>=10 else None)
            horizons = result["precursor_horizons"]["horizons"]
            self.assertEqual(horizons[0]["samples"], int(m >= 5))
            self.assertEqual(horizons[1]["samples"], int(m >= 10))
            self.assertEqual(horizons[2]["samples"], int(m >= 15))
        self.assertEqual([x["direction_correct"] for x in horizons], [1,1,1])
        self.assertTrue(all(x["direction_accuracy_pct"] is None for x in horizons))
        self.assertEqual(result["precursor_horizons"]["recent"][0]["status"], "FINISHED")
        self.assertEqual(result["precursor_horizons"]["recent"][0]["return_5m_pct"],
                         round(100*20/64000,5))

    def test_failed_early_warning_is_not_excluded_after_invalidation(self):
        self.record(self.start,64000,self.signal(self.start,64000,"UP"))
        for m in range(1, 16):
            self.record(self.start+m*60,64000-m*6)
        metrics = precursor_stats(self.execute)
        self.assertEqual([x["direction_incorrect"] for x in metrics["horizons"]],[1,1,1])
        self.assertIsNotNone(metrics["recent"][0]["invalidated_epoch"])

    def test_gap_does_not_invent_future_prices(self):
        self.record(self.start,64000,self.signal(self.start,64000))
        self.record(self.start+60,64010)
        result=self.record(self.start+180,64040)
        self.assertEqual(result["precursor_horizons"]["recent"][0]["status"],"GAP")
        for m in range(4, 17):
            result=self.record(self.start+m*60,64000+m*6)
        self.assertTrue(all(x["samples"] == 0 for x in result["precursor_horizons"]["horizons"]))

    def test_duplicate_early_warnings_suppressed_within_ten_minutes(self):
        self.record(self.start,64000,self.signal(self.start,64000))
        for m in range(1, 5):
            self.record(self.start+m*60,64000+m*2,
                        self.signal(self.start+m*60,64000+m*2))
        rows=self.execute("SELECT id FROM formation_precursors",fetch=True)
        self.assertEqual(len(rows),1)

    def test_invalid_source_and_wrong_reference_do_not_create_watches(self):
        wrong=self.signal(self.start,64000)
        wrong["source"]="Kraken BTC/USD"
        self.record(self.start,64000,wrong)
        bad=self.signal(self.start+60,64010)
        bad["precursor"]["reference_usd"]=999999
        self.record(self.start+60,64010,bad)
        self.assertEqual(len(self.execute("SELECT id FROM formation_precursors",fetch=True)),0)

    def test_ui_integration_is_read_only_and_has_sample_size_guard(self):
        text=(Path(__file__).resolve().parent/"terminal.html").read_text()
        self.assertIn("EARLIEST FORMATION WATCH",text)
        self.assertIn("x.sample_sufficient===true",text)
        self.assertIn("5/10/15m forward outcomes",text)
        self.assertIn("NO BREAKOUT",text)
        self.assertIn("NOT an established flag",text)


if __name__=="__main__":
    unittest.main()
