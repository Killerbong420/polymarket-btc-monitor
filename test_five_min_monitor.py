"""Regression coverage for five-minute early versus 2:30 BRTI snapshots."""
import asyncio
import sqlite3
from threading import RLock
import time
import unittest
from unittest.mock import AsyncMock, patch

import five_min_monitor as five


class MemoryDB:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = RLock()
        five.setup(self.execute)

    def execute(self, sql, args=(), fetch=False):
        with self.lock:
            cur = self.db.execute(sql, args)
            result = cur.fetchall() if fetch else None
            self.db.commit()
            return result

    def close(self):
        self.db.close()


class PhaseScoreTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB()

    def tearDown(self):
        self.db.close()

    def test_original_early_accuracy_is_unchanged_by_late_calls(self):
        early = "cpc-btc-updown-5m-2026-10-27-1900z"
        late = "cpc-btc-updown-5m-2026-10-27-1905z"
        self.db.execute("INSERT INTO five_min_predictions VALUES (?,?,?,?,?,?,?)",
                        (early,1,"UP",100,101,"WIN","UP"))
        self.db.execute("INSERT INTO five_min_late_predictions VALUES (?,?,?,?,?,?,?)",
                        (late,2,"DOWN",100,99,"LOSS","UP"))
        result = five.report(self.db.execute)
        self.assertEqual((result["wins"],result["losses"],result["graded"]),(1,0,1))
        self.assertEqual((result["late"]["wins"],result["late"]["losses"],result["late"]["graded"]),(0,0,0))
        self.assertEqual((result["late_legacy"]["wins"],result["late_legacy"]["losses"],result["late_legacy"]["graded"]),(0,1,1))

    def test_strict_late_accuracy_excludes_predictions_made_near_settlement(self):
        slug1="cpc-btc-updown-5m-2026-10-28-2000z"
        slug2="cpc-btc-updown-5m-2026-10-28-2005z"
        import datetime
        def end(slug):
            stamp=slug.split("btc-updown-5m-")[1]
            start=datetime.datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=datetime.timezone.utc)
            return (start+datetime.timedelta(minutes=5)).timestamp()
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                        (slug1,end(slug1)-135,"UP",100,101,"WIN","UP"))
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                        (slug2,end(slug2)-25,"DOWN",100,99,"LOSS","UP"))
        report=five.report(self.db.execute)
        self.assertEqual((report["late"]["wins"],report["late"]["losses"],report["late"]["graded"]),(1,0,1))
        self.assertEqual((report["late_legacy"]["wins"],report["late_legacy"]["losses"],report["late_legacy"]["graded"]),(1,1,2))

    def test_late_capture_audit_explains_zero_strict_calls_without_regrading(self):
        from datetime import datetime, timedelta, timezone
        start=datetime(2026,10,28,20,0,tzinfo=timezone.utc)
        # Two historical off-window captures and one genuine 2:30 capture.
        for minute,left,side,result in [(0,20,"UP","WIN"),(5,165,"DOWN","LOSS"),(10,135,"UP","WIN")]:
            round_start=start+timedelta(minutes=minute)
            slug="cpc-btc-updown-5m-"+round_start.strftime("%Y-%m-%d-%H%Mz")
            captured=(round_start+timedelta(minutes=5)).timestamp()-left
            self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                            (slug,captured,side,100,101,result,side if result=="WIN" else "UP"))
        result=five.strict_late_counts(self.db.execute)
        self.assertEqual((result["wins"],result["losses"],result["graded"]),(1,0,1))
        audit=result["capture_audit"]
        self.assertEqual(audit["sampled"],3)
        self.assertEqual((audit["within_window"],audit["before_window"],audit["after_window"],audit["invalid_timestamp"]),(1,1,1,0))
        self.assertEqual(sum(audit[key] for key in ("within_window","before_window","after_window","invalid_timestamp")),audit["sampled"])
        self.assertTrue(all("window" in row for row in audit["recent"]))
        # No grading mutation or timestamps changed.
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM five_min_late_predictions",fetch=True)[0][0],3)

    def test_late_capture_audit_does_not_relabel_invalid_market_or_pending(self):
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                        ("invalid-market",1234,"UP",100,101,"WIN","UP"))
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                        ("cpc-btc-updown-5m-2026-10-28-2010z",1235,"DOWN",100,99,"PENDING",None))
        audit=five.strict_late_counts(self.db.execute)
        self.assertEqual(audit["graded"],0)
        self.assertEqual(audit["capture_audit"]["sampled"],1)
        self.assertEqual(audit["capture_audit"]["invalid_timestamp"],1)
        self.assertEqual(audit["capture_audit"]["recent"][0]["window"],"invalid_timestamp")

    def test_reconciliation_scores_both_snapshots_with_matching_published_event(self):
        slug="cpc-btc-updown-5m-2026-10-27-1900z"
        for table,side in (("five_min_predictions","DOWN"),("five_min_late_predictions","UP")):
            self.db.execute(f"INSERT INTO {table} (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                            (slug,1,side,100,100))
        class Response:
            status_code=200
            def __init__(self,body): self.body=body
            def json(self):return self.body
        class Client:
            def __init__(self):self.paths=[]
            async def get(self,url,**kwargs):
                self.paths.append(url)
                if url.endswith("/settlement"):return Response({"settlement":"published"})
                return Response({"event":{"markets":[{"slug":slug,"status":"RESOLVED"}]}})
        client=Client()
        asyncio.run(five.score_pending(client,self.db.execute,lambda market,settlement:"UP"))
        rows={table:self.db.execute(f"SELECT result,winner FROM {table}",fetch=True)[0]
              for table in ("five_min_predictions","five_min_late_predictions")}
        self.assertEqual(rows["five_min_predictions"],("LOSS","UP"))
        self.assertEqual(rows["five_min_late_predictions"],("WIN","UP"))
        self.assertTrue(any("/events/slug/"+slug in url for url in client.paths))

    def test_only_fresh_official_index_can_be_called(self):
        reference={"status":"fresh","source":"Polymarket US BTC reference / BRTI"}
        with patch("five_min_monitor.forecast_settlement",return_value={
                    "status":"available","forecast_final_mean_usd":100.5}) as forecast:
            self.assertEqual(five.forecast_candidate(reference,{},100,150,0)[0]["direction"],"UP")
            self.assertIsNone(five.forecast_candidate({"status":"stale"}, {},100,150,0)[0])
            self.assertIsNone(five.forecast_candidate({"status":"fresh","source":"Coinbase"}, {},100,150,0)[0])
            self.assertEqual(forecast.call_count,1)



class OfficialPricePathPreviewTests(unittest.TestCase):
    def test_preview_uses_only_verified_past_brtI_and_has_bounded_tick_payload(self):
        start, end, now = 1000, 1300, 1140
        observations = [(t, 80000 + (t-start)*.05) for t in range(start, now)]
        observations += [(now+40, 200000)]  # future tick must never leak
        reference = {
            "status":"fresh","source":"Polymarket US BTC reference / BRTI",
            "window_start":start,"window_end":end,"points":observations}
        path = {"status":"available","quality":"OBSERVED",
                "current_reference_usd":80006.95,
                "forecast_final_mean_usd":80009.20,
                "downside_scenario_usd":79995,
                "upside_scenario_usd":80020,
                "opposing_short_long_slopes":True,"age_seconds":1}
        with patch("five_min_monitor.forecast_settlement",return_value=path) as estimator:
            observed=five.visual_forecast(reference,{},80004,160,now)
        self.assertEqual(observed["status"],"available")
        self.assertTrue(observed["reversal_warning"])
        self.assertEqual(observed["projected_gap_usd"],5.2)
        self.assertEqual(observed["final_minute_start_epoch"],end-60)
        self.assertEqual(observed["source"],"Polymarket US BTC reference / BRTI")
        self.assertLessEqual(len(observed["observed_points"]),five.VISUAL_MAX_POINTS+1)
        self.assertEqual(observed["observed_points"][-1][0],now-1)
        self.assertTrue(all(start<=t<=now for t,_ in observed["observed_points"]))
        self.assertNotIn("fair_price",observed)
        self.assertNotIn("contract_price",observed)
        self.assertNotIn("probability",observed)
        estimator.assert_called_once_with(reference,{},80004,160,now=now)

    def test_missing_or_invalid_index_cannot_be_visualized(self):
        ref={"status":"fresh","source":"Coinbase",
             "window_start":1000,"window_end":1300,"points":[(1100,80000)]}
        with patch("five_min_monitor.forecast_settlement") as estimator:
            self.assertEqual(five.visual_forecast(ref,{},80004,160,1140)["status"],"unavailable")
            ref["source"]="Polymarket US BTC reference / BRTI"
            ref["status"]="stale"
            self.assertEqual(five.visual_forecast(ref,{},80004,160,1140)["status"],"unavailable")
            estimator.assert_not_called()

    def test_preview_never_changes_the_frozen_early_or_late_prediction(self):
        db=MemoryDB()
        try:
            slug="cpc-btc-updown-5m-2026-10-28-2000z"
            db.execute("INSERT INTO five_min_predictions VALUES (?,?,?,?,?,?,?)",(slug,1,"UP",100,101,"WIN","UP"))
            db.execute("INSERT INTO five_min_late_predictions VALUES (?,?,?,?,?,?,?)",(slug,2,"DOWN",100,99,"LOSS","UP"))
            before=(db.execute("SELECT * FROM five_min_predictions",fetch=True),
                    db.execute("SELECT * FROM five_min_late_predictions",fetch=True))
            reference={"status":"fresh","source":"Polymarket US BTC reference / BRTI",
                       "window_start":1000,"window_end":1300,"points":[(1100,100),(1110,101)]}
            with patch("five_min_monitor.forecast_settlement",return_value={
                    "status":"available","current_reference_usd":101,
                    "forecast_final_mean_usd":102,"downside_scenario_usd":99,
                    "upside_scenario_usd":104}):
                five.visual_forecast(reference,{},100,160,1140)
            after=(db.execute("SELECT * FROM five_min_predictions",fetch=True),
                   db.execute("SELECT * FROM five_min_late_predictions",fetch=True))
            self.assertEqual(before,after)
        finally:
            db.close()




class DiscoveryConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_timeout_does_not_delay_independent_cpc_lookup(self):
        class Response:
            def __init__(self, status, body):
                self.status_code, self.body = status, body
            def json(self):
                return self.body

        class Client:
            def __init__(self):
                self.active = 0
                self.peak = 0
                self.calls = []
            async def get(self, url, **kwargs):
                self.calls.append((url, kwargs.get("timeout")))
                self.active += 1
                self.peak = max(self.peak, self.active)
                # Both lookups must have started before either completes.
                await asyncio.sleep(0)
                self.active -= 1
                slug = url.rsplit("/", 1)[-1]
                if not slug.startswith("cpc-"):
                    return Response(404, {})
                return Response(200, {"event": {"markets": [{
                    "slug": slug,
                    "assetPriceTerms": {"indexSymbol": "BRTI",
                                        "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
                                        "priceToBeat": 81000}
                }]}})

        client = Client()
        market, reason = await five.discover(client)
        self.assertIsNone(reason)
        self.assertEqual(client.peak, 2)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(timeout == 8 for _, timeout in client.calls))
        self.assertTrue(market["slug"].startswith("cpc-btc-updown-5m-"))
        self.assertEqual(market["assetPriceTerms"]["indexSymbol"], "BRTI")


class CheckpointLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db=MemoryDB()
        self.old_state=dict(five.state)
        self.old_market=five._last_market

    async def asyncTearDown(self):
        five.state.clear()
        five.state.update(self.old_state)
        five._last_market=self.old_market
        self.db.close()

    async def run_once(self, market, reference=None, client=None):
        async def discover(_):return market,None
        call={"direction":"DOWN","forecast":99.0}
        with (patch("five_min_monitor.discover",new=discover),
              patch("five_min_monitor.reference_prices",new=AsyncMock(return_value=reference or {"status":"fresh"})),
              patch("five_min_monitor.forecast_candidate",return_value=(call,None)),
              patch("five_min_monitor.score_pending",new=AsyncMock())):
            task=asyncio.create_task(five.loop(client,self.db.execute,lambda *_:"DOWN",{}))
            try:
                for _ in range(100):
                    if five.state.get("market")==market["slug"] and five.state.get("direction")=="DOWN":
                        break
                    await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    def test_verified_market_cache_expires_and_requires_official_brti_terms(self):
        end=time.time()+240
        market={"slug":"cpc-btc-updown-5m-test","end":end,
                "assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        self.assertTrue(five.reusable_verified_market(market,end-200))
        self.assertFalse(five.reusable_verified_market(market,end))
        self.assertFalse(five.reusable_verified_market(market,end-301))
        self.assertFalse(five.reusable_verified_market(
            {**market,"assetPriceTerms":{"priceToBeat":100}},end-200))
        self.assertFalse(five.reusable_verified_market(
            {**market,"assetPriceTerms":{"indexSymbol":"OTHER","priceToBeat":100}},end-200))

    async def test_verified_current_round_does_not_refetch_events_every_poll(self):
        end=time.time()+240
        market={"slug":"cpc-btc-updown-5m-2026-10-29-2350z","end":end,
                "assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        # A pre-existing early call avoids changing the scoring table in this test.
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (market["slug"],end-280,"UP",100,101))
        sleeps=0
        async def two_iterations(_seconds):
            nonlocal sleeps
            sleeps+=1
            if sleeps==2:
                raise asyncio.CancelledError()
        with (patch("five_min_monitor.discover",new=AsyncMock(return_value=(market,None))) as discovery,
              patch("five_min_monitor.reference_prices",new=AsyncMock(return_value={"status":"stale"})),
              patch("five_min_monitor.asyncio.sleep",new=two_iterations),
              patch("five_min_monitor.report",return_value={})):
            with self.assertRaises(asyncio.CancelledError):
                await five.live_loop(None,self.db.execute,{})
        self.assertEqual(sleeps,2)
        self.assertEqual(discovery.await_count,1)

    async def test_existing_early_call_gets_distinct_late_checkpoint(self):
        slug="cpc-btc-updown-5m-2026-10-28-1930z"
        end=time.time()+135
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-200,"UP",100,101))
        market={"slug":slug,"end":end,"assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        await self.run_once(market)
        stored=self.db.execute("SELECT direction,forecast FROM five_min_late_predictions WHERE market=?",(slug,),fetch=True)
        self.assertEqual(stored,[("DOWN",99)])
        self.assertEqual(five.state["phase"],"LATE_2M30")
        self.assertEqual(five.state["early_direction"],"UP")
        self.assertEqual(five.state["late_direction"],"DOWN")

    async def test_legacy_off_window_late_call_is_not_labeled_strict_230(self):
        slug="cpc-btc-updown-5m-2026-10-28-1932z"
        end=time.time()+100
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-240,"UP",100,101))
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast,result,winner) VALUES (?,?,?,?,?,?,?)",
                        (slug,end-100,"DOWN",100,99,"WIN","DOWN"))
        market={"slug":slug,"end":end,"assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        await self.run_once(market)
        self.assertEqual(five.state["phase"],"LEGACY_LATE")
        self.assertIn("excluded from strict accuracy",five.state["reason"])
        self.assertEqual(five.state["late_direction"],"DOWN")
        self.assertEqual(five.report(self.db.execute)["late"]["graded"],0)
        self.assertEqual(self.db.execute("SELECT result FROM five_min_late_predictions WHERE market=?",
                                         (slug,),fetch=True),[("WIN",)])

    async def test_valid_existing_late_call_keeps_strict_label_during_visual_refresh(self):
        slug="cpc-btc-updown-5m-2026-10-28-1934z"
        end=time.time()+130
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-250,"UP",100,101))
        self.db.execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-135,"DOWN",100,99))
        market={"slug":slug,"end":end,"assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        await self.run_once(market)
        self.assertEqual(five.state["phase"],"LATE_2M30")
        self.assertIn("Frozen 2:30-left forecast",five.state["reason"])

    async def test_no_mislabelled_late_call_at_100_seconds_remaining(self):
        slug="cpc-btc-updown-5m-2026-10-28-1932z"
        end=time.time()+100
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-200,"UP",100,101))
        market={"slug":slug,"end":end,"assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        await self.run_once(market)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM five_min_late_predictions",fetch=True)[0][0],0)

    async def test_no_backfilled_late_call_for_market_first_seen_after_checkpoint(self):
        slug="cpc-btc-updown-5m-2026-10-28-1935z"
        end=time.time()+100
        self.db.execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?)",
                        (slug,end-105,"DOWN",100,99))
        market={"slug":slug,"end":end,"assetPriceTerms":{"indexSymbol":"BRTI","priceToBeat":100}}
        await self.run_once(market)
        count=self.db.execute("SELECT COUNT(*) FROM five_min_late_predictions",fetch=True)[0][0]
        self.assertEqual(count,0)

    async def test_event_target_without_identity_fetches_authoritative_detail(self):
        slug="cpc-btc-updown-5m-2026-10-28-1940z"
        end=time.time()+280
        market={"slug":slug,"end":end,"assetPriceTerms":{"priceToBeat":100}}
        class Response:
            status_code=200
            def json(self):
                return {"market":{"slug":slug,"assetPriceTerms":{"indexSymbol":"BRTI",
                        "marketType":"ASSET_PRICE_MARKET_TYPE_UP_DOWN","priceToBeat":100}}}
        class Client:
            def __init__(self):self.paths=[]
            async def get(self,url,**kwargs):
                self.paths.append(url)
                return Response()
        client=Client()
        await self.run_once(market,client=client)
        self.assertEqual(five.state["status"],"running")
        self.assertEqual(five.state["direction"],"DOWN")
        self.assertTrue(any("/v1/markets/"+slug in p for p in client.paths))


if __name__=="__main__":
    unittest.main()
