"""Causal checks for early, read-only Doji pattern formation."""
import math
import unittest
from pathlib import Path
from unittest.mock import patch

from doji_watch import SOURCE, anticipate_doji


def sample(start, count, shape="dragonfly"):
    values = []
    for i in range(count):
        if shape == "dragonfly":
            p = 100 - 4 * math.sin(math.pi * i / (count - 1))
        elif shape == "gravestone":
            p = 100 + 4 * math.sin(math.pi * i / (count - 1))
        elif shape == "long_legged":
            p = 100 + 4 * math.sin(2 * math.pi * i / (count - 1))
        else:
            p = 100 + i * .1
        values.append((start + i, p))
    return values


def feed(points, source=SOURCE, status="fresh"):
    return {"status": status, "source": source, "points": points}


class DojiWatchTests(unittest.TestCase):
    def test_early_watch_precedes_close_without_probability_or_direction(self):
        minute = 1800000000
        for shape, expected in (("dragonfly", "DRAGONFLY_DOJI"),
                                ("gravestone", "GRAVESTONE_DOJI"),
                                ("long_legged", "LONG_LEGGED_DOJI")):
            with self.subTest(shape=shape):
                result = anticipate_doji(feed(sample(minute, 49, shape)), now=minute + 48.5)
                self.assertEqual(result["status"], "forming")
                self.assertEqual(result["pattern"], expected)
                self.assertGreater(result["seconds_until_close"], 0)
                self.assertEqual(result["candle_end_epoch"], minute + 60)
                self.assertFalse(result["influences_prediction"])
                self.assertFalse(result["probability_calibrated"])
                self.assertNotIn("direction", result)

    def test_no_signal_earlier_than_watch_window_or_after_close(self):
        minute = 1800000000
        early = anticipate_doji(feed(sample(minute, 35)), now=minute + 34.5)
        self.assertEqual(early["status"], "monitoring")
        self.assertNotIn("pattern", early)
        after = anticipate_doji(feed(sample(minute, 49)), now=minute + 61)
        self.assertNotEqual(after["status"], "forming")

    def test_trending_candle_not_mislabeled_as_doji(self):
        minute = 1800000000
        outcome = anticipate_doji(feed(sample(minute, 49, "trend")), now=minute + 48.5)
        self.assertEqual(outcome["status"], "monitoring")
        self.assertEqual(outcome["reason"], "no_doji_shape_yet")

    def test_missing_or_stale_ticks_refuse_early_watch(self):
        minute = 1800000000
        full = sample(minute, 49)
        for invalid in (
            feed(full, status="stale"),
            feed(full, source="Coinbase BTC/USD"),
            feed(full[:31]),
            feed(full[10:]),
            feed([v for v in full if v[0] % 12 == 0] + [full[-1]]),
        ):
            with self.subTest(invalid=str(invalid)[:50]):
                result = anticipate_doji(invalid, now=minute + 48.5)
                self.assertNotEqual(result["status"], "forming")
        result = anticipate_doji(feed(full), now=minute + 62)
        self.assertNotEqual(result["status"], "forming")

    def test_confirmation_only_after_minute_end(self):
        minute = 1800000060
        finished = sample(minute - 60, 60)
        before = anticipate_doji(feed(finished), now=minute - 1)
        self.assertNotEqual(before["status"], "confirmed")
        after = anticipate_doji(feed(finished), now=minute + 2)
        self.assertEqual(after["status"], "confirmed")
        self.assertEqual(after["candle_end_epoch"], minute)
        self.assertEqual(after["pattern"], "DRAGONFLY_DOJI")

    def test_no_confirmation_with_missing_end_of_previous_candle(self):
        minute = 1800000060
        result = anticipate_doji(feed(sample(minute - 60, 48)), now=minute + 2)
        self.assertNotEqual(result["status"], "confirmed")

    def test_doji_is_combined_with_formation_watch_no_extra_polls(self):
        html = (Path(__file__).resolve().parent / "terminal.html").read_text(encoding="utf-8")
        app = (Path(__file__).resolve().parent / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('id="dojiWatch"', html)
        self.assertEqual(html.count('id="structureWatch"'), 1)
        self.assertIn("const d=s.doji_watch||{}", html)
        self.assertIn("DOJI FORMING", html)
        self.assertIn("DOJI CONFIRMED", html)
        self.assertIn("FORMATION WATCH", html)
        self.assertIn("node.textContent=(doji||whale)?'FORMATION WATCH'+doji+whale", html)
        self.assertIn("setInterval(()=>{if(!document.hidden)renderPatterns()},1000)", html)
        self.assertNotIn("displayDoji()", html)
        self.assertIn('d["doji_watch"]=anticipate_doji(reference,now=time.time())', app)
        self.assertIn('"doji_watch":state.get("doji_watch")', app)
        segment = html.split("/* Render official BRTI Doji evidence inside Formation Watch", 1)[1].split("</script>", 1)[0]
        self.assertNotIn("fetch(", segment)
        self.assertNotIn("meterUpPct", segment)
        self.assertNotIn("meterDownPct", segment)



if __name__ == "__main__":
    unittest.main()
