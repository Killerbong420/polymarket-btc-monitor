"""Regression tests for forward-only missed-profit learning."""
import sqlite3
import time
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

import paper_learning as learn


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        def execute(sql,params=(),fetch=False):
            cur=self.db.execute(sql,params)
            if fetch:return cur.fetchall()
        self.execute=execute
        learn.setup(execute)
        execute("CREATE TABLE paper_positions (market TEXT)")
        start=int(time.time()//900)*900
        self.start=start
        self.market="cpc-btc-updown-15m-"+datetime.fromtimestamp(start,timezone.utc).strftime("%Y-%m-%d-%H%Mz")

    def tearDown(self):
        self.db.close()

    def quote(self,now,bid=.45,ask=.50):
        return {"status":"available","market":self.market,"fetched_epoch":now,
                "bid":bid,"ask":ask,"spread":ask-bid if bid is not None and ask is not None else None,
                "up_entry":ask,"down_entry":1-bid if bid is not None else None}

    def test_forward_quote_target_and_stop(self):
        initial=self.start+120
        with patch("paper_learning.time.time",return_value=initial):
            learn.observe(self.execute,self.quote(initial))
        before=self.execute("SELECT COUNT(*) FROM paper_learning_samples",fetch=True)[0][0]
        self.assertGreater(before,0)
        with patch("paper_learning.time.time",return_value=initial+10):
            learn.observe(self.execute,self.quote(initial+10,bid=.62,ask=.63))
        outcome=self.execute("SELECT status,exit_roi_pct FROM paper_learning_samples "
                             "WHERE market=? AND direction='UP' AND price_band='MID'",
                             (self.market,),fetch=True)[0]
        self.assertEqual(outcome[0],"TARGET")
        self.assertGreaterEqual(outcome[1],5)

    def test_loss_logged_before_later_rebound(self):
        initial=self.start+120
        with patch("paper_learning.time.time",return_value=initial):
            learn.observe(self.execute,self.quote(initial))
        with patch("paper_learning.time.time",return_value=initial+5):
            learn.observe(self.execute,self.quote(initial+5,bid=.34,ask=.35))
        with patch("paper_learning.time.time",return_value=initial+10):
            learn.observe(self.execute,self.quote(initial+10,bid=.65,ask=.66))
        result=self.execute("SELECT status,exit_roi_pct FROM paper_learning_samples "
                            "WHERE market=? AND direction='UP' AND price_band='MID'",
                            (self.market,),fetch=True)[0]
        self.assertEqual(result[0],"STOP")
        self.assertLessEqual(result[1],-8)

    def test_learning_requires_independent_markets_and_both_cohorts(self):
        for n in range(40):
            self.execute("INSERT INTO paper_learning_samples "
                "(id,market,direction,price_band,observed_epoch,entry_ask,quantity,"
                "entry_cost_cents,deadline_epoch,status,exit_roi_pct) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(n),"market-"+str(n),"UP","MID",n,.50,10,510,n+180,"TARGET",8))
        result=learn.policy(self.execute,"UP",.5)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["distinct_markets"],40)
        self.execute("UPDATE paper_learning_samples SET status='STOP',exit_roi_pct=-10 "
                     "WHERE observed_epoch<30")
        self.assertFalse(learn.policy(self.execute,"UP",.5)["enabled"])

    def test_missing_future_quote_is_not_counted_as_loss(self):
        initial=self.start+120
        with patch("paper_learning.time.time",return_value=initial):
            learn.observe(self.execute,self.quote(initial))
        with patch("paper_learning.time.time",return_value=initial+181):
            learn.observe(self.execute,self.quote(initial+181,bid=None,ask=None))
        status=self.execute("SELECT status FROM paper_learning_samples "
                            "WHERE direction='UP' AND price_band='MID'",fetch=True)[0][0]
        self.assertEqual(status,"UNOBSERVED")
        self.assertEqual(learn.stats(self.execute,"UP","MID")["distinct_markets"],0)

    def test_paper_exit_peak_regret_is_logged_once(self):
        learn.observe_position_peak(self.execute,"p1",self.market,500,580)
        learn.observe_position_peak(self.execute,"p1",self.market,500,550)
        cur=self.db.cursor()
        run=lambda c,pg,sql,args=():c.execute(sql,args)
        learn.finalize_position(cur,False,run,"p1",520,"TIME_EXIT")
        learn.finalize_position(cur,False,run,"p1",520,"TIME_EXIT")
        result=learn.report(self.execute)
        self.assertEqual(result["missed_actual_exit_count"],1)
        self.assertEqual(result["missed_actual_exit_profit_cents"],60)


if __name__=="__main__":
    unittest.main()
