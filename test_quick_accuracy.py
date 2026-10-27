"""Forward-only first-five-minute quick prediction logging and grading."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient

import app
from quick_forecast import quick_forecast
from quick_accuracy import SOURCE, make_entry, actual_tick_at, grade, summarize
from research import market_start


MARKET = "cpc-btc-updown-15m-2026-10-27-1800z"
START = int(market_start(MARKET))


def reference(last, slope=.22, omit=()):
    missing=set(omit)
    return {"status": "fresh", "source": SOURCE,
            "points": [(t, 80000+(t-START)*slope)
                       for t in range(START-60,int(last)+1) if t not in missing]}


class PureQuickAccuracyTests(unittest.TestCase):
    def test_capture_only_during_first_five_minutes_with_true_reference(self):
        now=START+40
        q=quick_forecast(reference(now),now=now)
        self.assertEqual(q["status"],"available")
        entry=make_entry(MARKET,START,now,q)
        self.assertEqual(entry["minute_slot"],0)
        self.assertEqual(entry["observed_epoch"],now)
        self.assertEqual(entry["target_epoch"],now+60)
        self.assertEqual(entry["direction"],"UP")
        self.assertIsNone(make_entry(MARKET,START,START+300,q))
        self.assertIsNone(make_entry(MARKET,START,now,{**q,"observed_epoch":START-1}))
        self.assertIsNone(make_entry(MARKET,START,now,{**q,"status":"unavailable"}))
        self.assertIsNone(make_entry(MARKET,START,now,{**q,"source":"Coinbase BTC-USD"}))

    def test_missing_future_tick_remains_pending_not_imputed(self):
        now=START+100
        ref=reference(now,omit={START+100})
        self.assertIsNone(actual_tick_at(ref,START+100,now))
        self.assertIsNone(actual_tick_at(reference(now),START+100,now-1))
        self.assertEqual(grade(80000,"UP",80000.22),"WIN")
        self.assertEqual(grade(80000,"DOWN",80000.22),"LOSS")
        self.assertEqual(grade(80000,"UP",80000),"TIE")

    def test_window_and_minute_breakdown_exclude_pending_ties(self):
        rows=[("WIN",0),("LOSS",0),("WIN",4)]
        out=summarize(rows,pending=2,no_call=3,ties=1)
        self.assertEqual(out["wins"],2)
        self.assertEqual(out["losses"],1)
        self.assertEqual(out["graded"],3)
        self.assertEqual(out["accuracy_pct"],66.7)
        self.assertEqual(out["minutes"][0]["accuracy_pct"],50.0)
        self.assertEqual(out["minutes"][4]["accuracy_pct"],100.0)
        self.assertEqual(out["pending"],2)


class QuickAccuracyDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.original=os.getcwd()
        self.tmp=tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.db=app.DB
        app.DB=""
        app._quick_seen_market=None
        app._quick_seen_slots.clear()
        app.setup()

    def tearDown(self):
        app.DB=self.db
        app._quick_seen_market=None
        app._quick_seen_slots.clear()
        os.chdir(self.original)
        self.tmp.cleanup()

    def test_one_snapshot_per_market_minute_and_verified_60s_grading(self):
        now=START+20
        ref=reference(now)
        q=quick_forecast(ref,now=now)
        app.track_first_five_quick(MARKET,ref,q,now)
        app.track_first_five_quick(MARKET,reference(now+3),q,now+3)
        self.assertEqual(app.execute("SELECT COUNT(*) FROM quick_predictions",fetch=True)[0][0],1)
        self.assertEqual(app.execute("SELECT result FROM quick_predictions",fetch=True)[0][0],"PENDING")
        # Exact 60s tick is not yet published: no backfilled win.
        app.track_first_five_quick(MARKET,reference(now+59),{},now+59)
        self.assertEqual(app.execute("SELECT result FROM quick_predictions",fetch=True)[0][0],"PENDING")
        app.track_first_five_quick(MARKET,reference(now+60),{},now+60)
        self.assertEqual(app.execute("SELECT result FROM quick_predictions",fetch=True)[0][0],"WIN")
        result=app.first_five_quick_report()
        self.assertEqual((result["wins"],result["losses"],result["pending"]),(1,0,0))
        self.assertEqual(result["minutes"][0]["graded"],1)
        self.assertEqual(app.execute("SELECT COUNT(*) FROM ai_decisions",fetch=True)[0][0],0)
        self.assertEqual(TestClient(app.app).get("/api/quick-accuracy").status_code,200)

    def test_first_five_slots_only_and_no_future_leaks(self):
        for minute in range(5):
            now=START+minute*60+30
            ref=reference(now)
            quick=quick_forecast(ref,now=now)
            app.track_first_five_quick(MARKET,ref,quick,now)
        late=START+5*60+30
        app.track_first_five_quick(MARKET,reference(late),quick_forecast(reference(late),now=late),late)
        slots=app.execute("SELECT minute_slot FROM quick_predictions ORDER BY minute_slot",fetch=True)
        self.assertEqual([x[0] for x in slots],[0,1,2,3,4])

    def test_missing_exact_outcome_is_not_a_loss(self):
        now=START+35
        ref=reference(now)
        q=quick_forecast(ref,now=now)
        app.track_first_five_quick(MARKET,ref,q,now)
        future=now+65
        missing=reference(future,omit={now+60})
        app.track_first_five_quick(MARKET,missing,{},future)
        self.assertEqual(app.execute("SELECT result FROM quick_predictions",fetch=True)[0][0],"PENDING")
        stats=app.first_five_quick_report()
        self.assertEqual((stats["graded"],stats["pending"]),(0,1))


if __name__=="__main__":
    unittest.main()
