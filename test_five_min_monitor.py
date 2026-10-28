"""Regression coverage for five-minute early versus 2:30 BRTI snapshots."""
import asyncio
import sqlite3
import time
import unittest
from unittest.mock import AsyncMock, patch

import five_min_monitor as five


class MemoryDB:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        five.setup(self.execute)

    def execute(self, sql, args=(), fetch=False):
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
