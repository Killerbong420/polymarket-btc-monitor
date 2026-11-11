"""Forward-only, persistent lifecycle tracking for independently observed chart patterns."""
import sqlite3
import unittest
from pathlib import Path

from pattern_lifecycle import setup_tables, record_completed_bar, record_coinbase_sequence, COINBASE_SOURCE


class PatternLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.market = "cpc-btc-updown-15m-2026-11-10-2100z"
        self.source = COINBASE_SOURCE
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

    def test_resolved_only_follow_through_by_pattern_with_small_sample_guard(self):
        from pattern_lifecycle import _recent
        # Known forward-recorded labels; this audit must never conflate
        # invalidated/pending/expired with profitable Polymarket trades.
        for i in range(22):
            pattern = "DOUBLE_TOP" if i<14 else "BULL_FLAG"
            phase = (("COMPLETED" if i<8 else "INVALIDATED" if i<10
                      else "EXPIRED" if i<12 else "FORMING") if i<14
                     else ("COMPLETED" if i<17 else "INVALIDATED" if i<18 else "EXPIRED"))
            self.execute("""INSERT INTO pattern_lifecycle (
              id,market,source,pattern,anchor_epoch,direction,phase,initial_phase,
              first_seen_epoch,last_bar_epoch,trigger_usd,invalidation_usd,
              objective_usd,initial_fit_pct,result)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (str(i),self.market,self.source,pattern,self.first+i*60,
               1 if pattern=="BULL_FLAG" else -1,phase,"FORMING",
               float(self.first+i*60),self.first+i*60,100,110,90,80,
               "MEASURED_OBJECTIVE_REACHED" if phase=="COMPLETED" else "PENDING"))
        info=_recent(self.execute,self.market)["follow_through"]
        stats={x["pattern"]:x for x in info["by_pattern"]}
        self.assertEqual(info["cohort_limit"],200)
        top=stats["DOUBLE_TOP"]
        self.assertEqual((top["completed"],top["invalidated"],top["resolved"]),(8,2,10))
        self.assertEqual(top["objective_rate_pct"],80.0)
        self.assertTrue(top["sample_sufficient"])
        flag=stats["BULL_FLAG"]
        self.assertEqual((flag["completed"],flag["invalidated"],flag["resolved"]),(3,1,4))
        self.assertIsNone(flag["objective_rate_pct"])
        self.assertFalse(flag["sample_sufficient"])
        self.assertEqual(stats["DOUBLE_BOTTOM"]["resolved"],0)
        self.assertIsNone(stats["DOUBLE_BOTTOM"]["objective_rate_pct"])

    def test_follow_through_display_is_read_only_and_requires_sample_size(self):
        html=(Path(__file__).resolve().parent/"terminal.html").read_text(encoding="utf-8")
        self.assertIn('id="formationFollowThrough"',html)
        self.assertIn("drawFollowThrough(s.pattern_lifecycle?.follow_through)",html)
        self.assertIn("x.sample_sufficient===true&&count>=10",html)
        self.assertIn("NOT win odds",html)
        block=html.split("function drawFollowThrough(info)",1)[1].split("function renderPatterns()",1)[0]
        self.assertNotIn("fetch(",block)
        self.assertNotIn("meterUpPct",block)

    def test_early_warning_lead_time_and_pre_breakout_invalidations(self):
        from pattern_lifecycle import _recent
        # 8 reached breakout after early warning, 2 invalidated before,
        # 2 expired/no complete observation (excluded from denominator).
        for i in range(12):
            phase="COMPLETED" if i<8 else "INVALIDATED" if i<10 else "EXPIRED"
            first=self.first+i*60
            br=first+120 if i<8 else None
            invalid=first+60 if 8<=i<10 else None
            expired=first+60 if i>=10 else None
            self.execute("""INSERT INTO pattern_lifecycle (
              id,market,source,pattern,anchor_epoch,direction,phase,initial_phase,
              first_seen_epoch,last_bar_epoch,breakout_epoch,invalidated_epoch,
              expired_epoch,trigger_usd,invalidation_usd,objective_usd,
              initial_fit_pct,result)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (str(i),self.market,self.source,"DOUBLE_TOP",first-60,-1,
               phase,"FORMING",float(first),first+120,br,invalid,
               expired,100,110,90,80,"PENDING"))
        metrics=_recent(self.execute,self.market)["early_warning_stats"]
        top=next(x for x in metrics["by_pattern"] if x["pattern"]=="DOUBLE_TOP")
        self.assertEqual(top["early_warnings"],12)
        self.assertEqual(top["breakouts"],8)
        self.assertEqual(top["invalidated_before_breakout"],2)
        self.assertEqual(top["expired_or_open"],2)
        self.assertEqual(top["lead_samples"],8)
        self.assertEqual(top["avg_lead_seconds"],120.0)
        self.assertEqual(top["early_breakout_rate_pct"],80.0)
        self.assertTrue(top["rate_sample_sufficient"])
        self.assertIsNone(next(x for x in metrics["by_pattern"]
                               if x["pattern"]=="BULL_FLAG")["early_breakout_rate_pct"])
        # A setup seen already breaking out must not be counted as early.
        self.execute("""INSERT INTO pattern_lifecycle (
          id,market,source,pattern,anchor_epoch,direction,phase,initial_phase,
          first_seen_epoch,last_bar_epoch,breakout_epoch,trigger_usd,
          invalidation_usd,objective_usd,initial_fit_pct,result)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          ("late",self.market,self.source,"DOUBLE_TOP",self.first+1000,-1,
           "COMPLETED","BREAKOUT",float(self.first+1000),self.first+1120,
           self.first+1000,100,110,90,80,"PENDING"))
        top2=next(x for x in _recent(self.execute,self.market)["early_warning_stats"]["by_pattern"]
                  if x["pattern"]=="DOUBLE_TOP")
        self.assertEqual(top2["early_warnings"],12)

    def test_early_warning_panel_is_read_only(self):
        html=(Path(__file__).resolve().parent/"terminal.html").read_text(encoding="utf-8")
        self.assertIn('id="formationEarlyWarning"',html)
        self.assertIn("drawEarlyWarnings(s.pattern_lifecycle?.early_warning_stats)",html)
        self.assertIn("n+fail>=10",html)
        self.assertIn("avg_lead_seconds",html)
        part=html.split("function drawEarlyWarnings(info)",1)[1].split("function drawFollowThrough(info)",1)[0]
        self.assertNotIn("fetch(",part)
        self.assertNotIn("meterDownPct",part)

    def test_coinbase_history_excludes_old_kraken_rows(self):
        self.record(self.first,105)
        # Old Kraken records remain untouched in DB but must be excluded from
        # the Coinbase-only Formation Watch and statistical denominators.
        self.execute("""INSERT INTO pattern_lifecycle (
          id,market,source,pattern,anchor_epoch,direction,phase,initial_phase,
          first_seen_epoch,last_bar_epoch,trigger_usd,invalidation_usd,
          objective_usd,initial_fit_pct,result)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          ("old-kraken",self.market,"Kraken BTC/USD","BULL_FLAG",self.first,
           1,"COMPLETED","FORMING",float(self.first),self.first+120,100,90,110,75,
           "MEASURED_OBJECTIVE_REACHED"))
        from pattern_lifecycle import _recent
        info=_recent(self.execute,self.market)
        self.assertEqual(info["source"],COINBASE_SOURCE)
        self.assertEqual(len(info["recent"]),1)
        self.assertTrue(all(x["source"]==COINBASE_SOURCE for x in info["recent"]))
        flags=next(x for x in info["follow_through"]["by_pattern"] if x["pattern"]=="BULL_FLAG")
        self.assertEqual(flags["resolved"],0)
        self.assertEqual(record_completed_bar(self.execute,self.market,
                         self.signal(source="Kraken BTC/USD"),self.sample(self.first+60,102),
                         now=self.first+62)["reason"],"coinbase_candles_required")

    def test_verified_recent_coinbase_sequence_repairs_one_minute_gap(self):
        self.record(self.first,105)
        # The prior minute was late to the backend, but is still a genuine
        # completed Coinbase OHLC candle in this response.
        late_bar=self.sample(self.first+60,103)
        latest_bar=self.sample(self.first+120,98)
        result=record_coinbase_sequence(
            self.execute,self.market,self.signal(status="monitoring"),
            [late_bar,latest_bar],now=self.first+122)
        saved=result["recent"][0]
        self.assertEqual(saved["phase"],"BREAKOUT")
        self.assertEqual(saved["confirmed_epoch"],self.first+60)
        self.assertEqual(saved["breakout_epoch"],self.first+120)
        self.assertIsNone(saved["expired_epoch"])
        self.assertEqual(len(self.rows()),1)

    def test_missing_coinbase_minute_still_expires_and_no_fake_replay(self):
        self.record(self.first,105)
        result=record_coinbase_sequence(
            self.execute,self.market,self.signal(status="monitoring"),
            [self.sample(self.first+120,89)],now=self.first+122)
        self.assertEqual(result["recent"][0]["result"],"OBSERVATION_GAP")
        self.assertEqual(result["recent"][0]["phase"],"EXPIRED")

    def test_recovery_does_not_create_old_signals_or_use_kraken(self):
        start=self.first+180
        history=[self.sample(start-120,105),self.sample(start-60,104),self.sample(start,103)]
        current=self.signal(anchor_epoch=start-60)
        out=record_coinbase_sequence(self.execute,self.market,current,history,now=start+1)
        self.assertEqual(len(out["recent"]),1)
        self.assertEqual(out["recent"][0]["first_seen_epoch"],float(start+1))
        # No new Kraken setups even when its OHLC candles look valid.
        wrong=self.signal(source="Kraken BTC/USD")
        unavailable=record_coinbase_sequence(self.execute,self.market,wrong,
                                               history,now=start+2)
        self.assertEqual(unavailable["reason"],"coinbase_candles_required")

    def test_out_of_order_or_duplicate_candles_rejected(self):
        latest=self.sample(self.first,105)
        previous=self.sample(self.first-60,104)
        for bars in ([latest,previous],[latest,latest]):
            with self.subTest(bars=bars):
                out=record_coinbase_sequence(self.execute,self.market,
                    self.signal(),bars,now=self.first+1)
                self.assertEqual(out["reason"],"invalid_coinbase_sequence")
                self.assertEqual(len(self.rows()),0)

    def test_integration_keeps_main_forecast_untouched(self):
        root=Path(__file__).resolve().parent
        app=(root/"app.py").read_text()
        html=(root/"terminal.html").read_text()
        self.assertIn("from pattern_lifecycle import setup_tables",app)
        self.assertIn("record_coinbase_sequence,execute,slug,context,formation_candles,time.time()",app)
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
