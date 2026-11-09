"""No-hindsight minute cohorts, source parity, and safe quote freshness tests."""
import json
import unittest
from unittest.mock import patch

import app

from minute_research import (SOURCE, VERSION, book_freshness,
                             minute_observation, evaluate_minutes)
from research import market_start

SLUG = "btc-updown-15m-2026-11-09-2300z"
START = market_start(SLUG)
NOW = START + 125
TARGET = 99.0
PRICE = 100.0
GAP = round(100*(PRICE/TARGET-1), 5)


def reference(now=NOW, price=PRICE, age=3):
    return {"status":"fresh", "source":SOURCE, "price":price,
            "price_epoch":now-age, "age_seconds":age,
            "points":[(int(now)-i, price) for i in range(20,-1,-1)]}


def behavior():
    return {"status":"available","source":SOURCE, "is_reference":True,
            "target_gap_pct":GAP, "elapsed_seconds":125, "price_age_seconds":3}


def path():
    return {"status":"available","quality":"OBSERVED",
            "target_usd":TARGET, "current_reference_usd":PRICE,
            "volatility_reference_usd_per_min":2.0}


def quote(now=NOW):
    return {"status":"available","market":SLUG,
            "bid":.51,"ask":.54,"fetched_epoch":now-3}


def make(now=NOW, **kwargs):
    data=dict(market=SLUG,seconds_left=900-(now-START),
              official_target=TARGET,reference=reference(now=now),
              behavior=behavior(),path=path(),quote=quote(now),now=now)
    data.update(kwargs)
    return minute_observation(**data)


class MinuteEvidenceTests(unittest.TestCase):
    def test_future_only_minute_observation_and_theoretical_chance(self):
        s=make()
        self.assertEqual(s["status"],"captured")
        self.assertEqual(s["minute_slot"],2)
        self.assertEqual(s["market_minute"],3)
        self.assertEqual(s["input_parity"],"MATCH")
        self.assertAlmostEqual(s["gap_pct_from_official"],100*(100/99-1),places=5)
        self.assertGreater(s["theoretical_up_probability"],.5)
        self.assertFalse(s["theoretical_probability_calibrated"])
        self.assertTrue(s["no_orders"])
        self.assertEqual(s["book"]["status"],"fresh")

    def test_reject_spot_proxy_stale_ticks_and_wrong_round(self):
        s=make(reference={**reference(),"source":"Coinbase spot"})
        self.assertEqual(s["status"],"unavailable")
        self.assertEqual(make(reference=reference(age=17))["status"],"unavailable")
        self.assertEqual(make(market="btc-updown-5m-2026-11-09-2300z")["status"],"unavailable")
        self.assertEqual(make(seconds_left=300)["reason"],"market_clock_mismatch")

    def test_parity_blocks_any_mismatched_theoretical_score(self):
        mismatched=make(behavior={**behavior(),"target_gap_pct":4.0})
        self.assertEqual(mismatched["input_parity"],"MISMATCH")
        self.assertIsNone(mismatched["theoretical_up_probability"])
        bad_path=make(path={**path(),"target_usd":99.1})
        self.assertEqual(bad_path["input_parity"],"MISMATCH")
        self.assertEqual(make(validation={"material":True})["input_parity"],"MISMATCH")
        self.assertEqual(make(path={"status":"unavailable"})["input_parity"],"UNAVAILABLE")

    def test_quote_stale_crossed_wrong_market_and_rapid_move(self):
        self.assertEqual(book_freshness(quote(),SLUG,reference(),NOW)["status"],"fresh")
        old={**quote(),"fetched_epoch":NOW-12}
        self.assertEqual(book_freshness(old,SLUG,reference(),NOW)["status"],"stale")
        crossed={**quote(),"bid":.6,"ask":.5}
        self.assertEqual(book_freshness(crossed,SLUG,reference(),NOW)["status"],"unavailable")
        self.assertEqual(book_freshness(quote(),"different",reference(),NOW)["status"],"unavailable")
        self.assertEqual(book_freshness({**quote(),"fetched_epoch":NOW+10},SLUG,reference(),NOW)["status"],"unavailable")
        points=[(int(NOW)-15,99.80),(int(NOW)-2,100.0)]
        jump={**reference(),"points":points,"price_epoch":int(NOW)-2}
        q=book_freshness(quote(),SLUG,jump,NOW)
        self.assertEqual(q["status"],"rapid_move_recheck")
        self.assertFalse(q["rapid_move_is_quote_staleness"])
        self.assertFalse(q["orders_allowed"])

    def test_evaluation_only_uses_official_future_labels_and_no_duplicates(self):
        valid=make()
        wrong={**valid,"input_parity":"MISMATCH","theoretical_up_probability":None}
        other={**valid,"theoretical_up_probability":None}
        b="a-"+SLUG
        c="b-"+SLUG
        d="c-"+SLUG
        e="d-"+SLUG
        tampered="e-"+SLUG
        corrupt={**valid, "market":tampered, "gap_pct_from_official":5.0}
        rows=[(SLUG,2,json.dumps(valid),"UP"),
              (SLUG,2,json.dumps(valid),"UP"),
              (b,2,json.dumps({**wrong,"market":b}),"DOWN"),
              (c,2,json.dumps({**other,"market":c}),"UP"),
              (d,2,json.dumps({**valid,"market":d}),None),
              (e,2,"invalid JSON","DOWN"),
              (tampered,2,json.dumps(corrupt),"DOWN")]
        report=evaluate_minutes(rows)
        g=report["minutes"][0]
        self.assertEqual(g["officially_settled_samples"],4)
        self.assertEqual(g["graded"],1)
        self.assertEqual(g["input_parity_mismatches"],1)
        self.assertEqual(g["archived_replay_mismatches"],1)
        self.assertEqual(g["missing_or_unrated"],1)
        self.assertFalse(report["auto_promote"])
        self.assertFalse(g["sample_sufficient_for_review"])
        self.assertEqual(len(g["reliability"]),1)

    def test_no_posthoc_price_or_betting_outcomes_used_in_model(self):
        future=make(now=START+500, behavior={**behavior(),"elapsed_seconds":500})
        self.assertEqual(future["minute_slot"],8)
        self.assertTrue(1 <= future["market_minute"] <= 15)
        self.assertEqual(future["input_parity"],"MATCH")

    def test_persistent_capture_once_per_market_minute_with_no_rewrite(self):
        sample=make()
        with patch.object(app, "_minute_research_saved_key", None), \
             patch.object(app, "_minute_research_prune_at", 10**12), \
             patch.object(app, "execute") as execute:
            self.assertTrue(app.save_minute_observation(sample))
            self.assertFalse(app.save_minute_observation(sample))
            self.assertEqual(execute.call_count, 1)
            sql, values = execute.call_args.args[:2]
            self.assertIn("ON CONFLICT(id) DO NOTHING", sql)
            self.assertEqual(values[0], SLUG+":2")
            self.assertEqual(json.loads(values[4])["input_parity"], "MATCH")

    def test_endpoints_are_read_only_and_officially_scored(self):
        sample=make()
        row=(SLUG,2,json.dumps(sample),"UP")
        with patch.object(app, "iter_query_rows", return_value=[row]) as query:
            report=app.minute_calibration_report()
        self.assertEqual(report["minutes"][0]["graded"],1)
        sql=query.call_args.args[0]
        self.assertIn("final_reference_source='OFFICIAL_MARKET_SETTLEMENT'",sql)
        self.assertIn("MIN(winner)=MAX(winner)",sql)
        current={"market":SLUG,"market_quote_guard":{"status":"fresh"},
                 "minute_research_latest":{"status":"captured","input_parity":"MATCH"}}
        with patch.object(app,"state",current), \
             patch.object(app,"iter_query_rows",side_effect=AssertionError("unexpected DB")):
            status=app.live_integrity()
        self.assertEqual(status["quote"]["status"],"fresh")
        self.assertFalse(status["orders_enabled"])


if __name__ == "__main__":
    unittest.main()
