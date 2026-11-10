"""Regression tests for independent and causal early-pattern mapping."""
import math
import unittest
from pathlib import Path

from early_pattern_context import analyze_early_patterns


def make_bars(pole=1, extend=None):
    """Contiguous completed exchange candles, with a pole and 4-bar pullback."""
    closes=[1000 + math.sin(i/2) for i in range(35)]
    start=closes[-1]
    closes += [start + pole*10*i for i in range(1,6)]
    top=closes[-1]
    closes += [top - pole*4*i for i in range(1,5)]
    if extend:
        closes += extend(top)
    bars=[]
    for i,close in enumerate(closes):
        op=closes[i-1] if i else close
        bars.append([1800000000+60*i,min(op,close)-1,max(op,close)+1,op,close,10])
    return bars


class EarlyPatternContextTests(unittest.TestCase):
    def test_up_and_down_flags_are_forming_before_breakout(self):
        for sign, kind in ((1,"BULL_FLAG"),(-1,"BEAR_FLAG")):
            with self.subTest(sign=sign):
                bars=make_bars(sign)
                original=[v.copy() for v in bars]
                result=analyze_early_patterns(bars)
                self.assertEqual(result["status"],"watch")
                self.assertEqual(result["pattern"],kind)
                self.assertEqual(result["phase"],"FORMING")
                self.assertTrue(result["trigger_usd"] > result["invalidation_usd"] if sign>0
                                else result["trigger_usd"] < result["invalidation_usd"])
                self.assertFalse(result["probability_calibrated"])
                self.assertFalse(result["changes_primary_prediction"])
                self.assertNotIn("win_probability",result)
                self.assertNotIn("predicted_outcome",result)
                self.assertEqual(bars,original)

    def test_breakout_then_retest_are_distinguished(self):
        for sign in (1,-1):
            with self.subTest(sign=sign):
                baseline=make_bars(sign)
                top=baseline[-5][4]
                # Break through the flag edge, then wick back into the prior
                # resistance/support, but close on the breakout side.
                breakout=top+sign*3
                bars=make_bars(sign,extend=lambda _: [breakout])
                bars[-1][5]=20
                b=analyze_early_patterns(bars)
                self.assertEqual(b["phase"],"BREAKOUT_WATCH")
                self.assertTrue(b["volume_confirmed"])
                trigger=b["trigger_usd"]
                last_close=trigger+sign*2
                bars.append([bars[-1][0]+60,
                             min(trigger-sign*1.5,last_close,breakout)-1 if sign>0 else min(last_close,breakout)-1,
                             max(trigger+sign*1.5,last_close,breakout)+1 if sign<0 else max(last_close,breakout)+1,
                             breakout,last_close,10])
                retest=analyze_early_patterns(bars)
                self.assertEqual(retest["phase"],"RETEST_HELD")

    def test_invalidated_flag_does_not_get_called_a_breakout(self):
        for sign in (1,-1):
            with self.subTest(sign=sign):
                bars=make_bars(sign)
                end=bars[-5][4]
                invalid=end-sign*42
                last=bars[-1]
                bars.append([last[0]+60,min(last[4],invalid)-1,max(last[4],invalid)+1,last[4],invalid,10])
                result=analyze_early_patterns(bars)
                self.assertEqual(result["phase"],"INVALIDATED")

    def test_missing_stale_and_malformed_candles_are_unavailable(self):
        bars=make_bars()
        self.assertEqual(analyze_early_patterns(bars[:20])["status"],"unavailable")
        broken=[b.copy() for b in bars]
        broken[-2][0]+=60
        self.assertEqual(analyze_early_patterns(broken)["reason"],"missing_completed_candles")
        bad=[b.copy() for b in bars]
        bad[-1][4]=float("nan")
        self.assertEqual(analyze_early_patterns(bad)["reason"],"invalid_ohlcv")

    def test_backend_carries_independent_context_and_ui_has_no_extra_polling(self):
        root=Path(__file__).resolve().parent
        app=(root/"app.py").read_text(encoding="utf-8")
        html=(root/"terminal.html").read_text(encoding="utf-8")
        self.assertIn('from early_pattern_context import analyze_early_patterns',app)
        self.assertEqual(app.count('"early_patterns":analyze_early_patterns('),2)
        self.assertIn('id="structureWatch"',html)
        section=html.split("/* Independent, read-only pattern observations;",1)[1].split("</script>",1)[0]
        self.assertNotIn("fetch(",section)
        self.assertNotIn("meterUpPct",section)
        self.assertNotIn("meterDownPct",section)


if __name__=="__main__":
    unittest.main()
