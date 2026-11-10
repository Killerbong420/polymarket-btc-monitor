"""Forward-only, persistent lifecycle tracking for independently observed chart patterns."""
import sqlite3
import unittest
from pathlib import Path

from pattern_lifecycle import setup_tables, record_completed_bar


class PatternLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.market = "cpc-btc-updown-15m-2026-11-10-2100z"
        self.source = "Kraken BTC/USD"
        self.first = 1800000060
        setup_tables(self.execute)

    def tearDown(self):
        self.db.close()

    def execute(self, sql, params=(), fetch=False):
        c = self.db.execute(sql,params)
        rows=c.fetchall() if fetch else None
        self.db.commit()
        return rows

    def signal(self, **updates):
        sample={"status":"watch", "source":self.source,
                "pattern":"DOUBLE_TOP", "phase":"FORMING", "anchor_epoch":self.first-60,
                "trigger_usd":100.0, "invalidation_usd":110.0,
                "pattern_match_score_pct":81,"score_basis":"heuristic_geometry_v1"}
        sample.update(updates)
        return sample

    def sample(self, epoch, close, open_price=None, low=None, high=None):
        op=close if open_price is None else open_price
        lo=min(close,op)-1 if low is None else low
        hi=max(close,op)+1 if high is None else high
        return [epoch-60,lo,hi,op,close,1]

    def record(self, epoch, close, signal=None, **candle_options):
        return record_completed_bar(self.execute,self.market,
                                    self.signal() if signal is None else signal,
                                    self.sample(epoch,close,**candle_options),
                                    now=epoch+2)

    def rows(self):
        return self.execute("""SELECT phase,initial_phase,first_seen_epoch,
          confirmed_epoch,breakout_epoch,completed_epoch,invalidated_epoch,
          initial_fit_pct,result FROM pattern_lifecycle""",fetch=True)

    def test_double_top_completion_is_not_rewritten_by_later_rebound(self):
        first=self.record(self.first,105)
        self.assertEqual(first["recent"][0]["phase"],"FORMING")
        self.assertEqual(first["recent"][0]["initial_fit_pct"],81)
        confirmed=self.record(self.first+60,103,self.signal(status="monitoring"))
        self.assertEqual(confirmed["recent"][0]["phase"],"CONFIRMED")
        self.assertEqual(confirmed["recent"][0]["confirmed_epoch"],self.first+60)
        breakout=self.record(self.first+120,98,self.signal(status="monitoring"))
        self.assertEqual(breakout["recent"][0]["phase"],"BREAKOUT")
        self.assertEqual(breakout["recent"][0]["breakout_epoch"],self.first+120)
        complete=self.record(self.first+180,89,self.signal(status="monitoring"))
        done=complete["recent"][0]
        self.assertEqual(done["phase"],"COMPLETED")
        self.assertEqual(done["result"],"MEASURED_OBJECTIVE_REACHED")
        self.assertEqual(done["completed_epoch"],self.first+180)
        self.assertEqual(done["objective_usd"],90)
        self.assertEqual(done["initial_fit_pct"],81)
        # Same observed formation, but later reclassified as invalidated.
        rebound=self.record(self.first+240,112,self.signal(phase="INVALIDATED"))
        self.assertEqual(rebound["recent"][0]["phase"],"COMPLETED")
        self.assertIsNone(rebound["recent"][0]["invalidated_epoch"])
        self.assertEqual(len(rebound["recent"]),1)

    def test_invalidation_before_target_is_not_reported_as_completed(self):
        self.record(self.first,105)
        invalid=self.record(self.first+60,111,self.signal(phase="INVALIDATED"))
        row=invalid["recent"][0]
        self.assertEqual(row["phase"],"INVALIDATED")
        self.assertEqual(row["result"],"INVALIDATION_LEVEL_CLOSED")
        self.assertIsNone(row["completed_epoch"])
        self.record(self.first+120,89,self.signal(status="monitoring"))
        self.assertEqual(self.rows()[0][0],"INVALIDATED")

    def test_not_completed_on_first_breakout_without_follow_through(self):
        initial=self.record(self.first,99,self.signal(phase="NECKLINE_BREAK_WATCH"))
        self.assertEqual(initial["recent"][0]["phase"],"BREAKOUT")
        self.assertEqual(initial["recent"][0]["initial_phase"],"BREAKOUT")
        self.assertIsNone(initial["recent"][0]["confirmed_epoch"])
        next_bar=self.record(self.first+60,99,self.signal(status="monitoring"))
        self.assertEqual(next_bar["recent"][0]["phase"],"BREAKOUT")
        self.assertIsNone(next_bar["recent"][0]["completed_epoch"])

    def test_gap_expires_without_fabricated_completion(self):
        self.record(self.first,105)
        after_gap=self.record(self.first+120,89,self.signal(status="monitoring"))
        row=after_gap["recent"][0]
        self.assertEqual(row["phase"],"EXPIRED")
        self.assertEqual(row["result"],"OBSERVATION_GAP")
        self.assertIsNone(row["completed_epoch"])

    def test_distinct_episodes_are_preserved_without_duplicate_updates(self):
        self.record(self.first,105)
        self.record(self.first,105)
        self.assertEqual(len(self.rows()),1)
        newer=self.signal(anchor_epoch=self.first+120,pattern_match_score_pct=70)
        self.record(self.first+180,104,newer)
        rows=self.rows()
        self.assertEqual(len(rows),2)
        self.assertEqual({r[7] for r in rows},{70,81})

    def test_invalid_input_is_not_logged(self):
        broken=self.signal(trigger_usd=130)
        out=self.record(self.first,105,broken)
        self.assertEqual(out["status"],"collecting")
        self.assertEqual(len(self.rows()),0)
        stale=record_completed_bar(self.execute,self.market,self.signal(),
                                   self.sample(self.first,105),now=self.first+120)
        self.assertEqual(stale["reason"],"stale_completed_candle")
        self.assertEqual(len(self.rows()),0)
        wrong_market=record_completed_bar(self.execute,"wrong",self.signal(),
                                          self.sample(self.first,105),now=self.first+2)
        self.assertEqual(wrong_market["status"],"unavailable")

    def test_integration_keeps_main_forecast_untouched(self):
        root=Path(__file__).resolve().parent
        app=(root/"app.py").read_text()
        html=(root/"terminal.html").read_text()
        self.assertIn("from pattern_lifecycle import setup_tables",app)
        self.assertIn("record_completed_bar,execute,slug,context,candle,time.time()",app)
        self.assertIn('"pattern_lifecycle":state.get("pattern_lifecycle")',app)
        self.assertIn('id="patternLifecycleRows"',html)
        self.assertIn("lastTimelineKey",html)
        self.assertIn("MEASURED_OBJECTIVE_REACHED",Path(__file__).with_name("pattern_lifecycle.py").read_text())
        block=html.split("/* Independent, read-only pattern observations;",1)[1].split("</script>",1)[0]
        self.assertNotIn("fetch(",block)
        self.assertNotIn("meterUpPct",block)
        self.assertNotIn("meterDownPct",block)


if __name__ == "__main__":
    unittest.main()
