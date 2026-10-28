"""Regression tests for independent live feed and settlement task responsiveness."""
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
import app


class LiveFeedTests(unittest.TestCase):
    def test_live_api_does_not_touch_database(self):
        old=dict(app.state)
        try:
            app.state.update(status="running",market="btc-updown-15m-example",
                             updated=datetime.now(timezone.utc).isoformat(),
                             signal="DOWN",external={"price":81000.0},
                             final_15m={"market":"btc-updown-15m-example","seconds_left":280},
                             last_halfway_ai={"market":"btc-updown-15m-example",
                                              "direction":"DOWN","updated":"2026-10-26T19:52:34Z"})
            with patch("app.execute",side_effect=RuntimeError("Database is slow")):
                client=TestClient(app.app)
                live=client.get("/api/live")
                self.assertEqual(live.status_code,200)
                result=live.json()
                self.assertEqual(result["status"],"running")
                self.assertEqual(result["signal"],"DOWN")
                self.assertEqual(result["last_halfway_ai"]["direction"],"DOWN")
                self.assertIsInstance(result["server_epoch"],float)
                status=client.get("/api/status").json()
                self.assertIn("database_error",status)
        finally:
            app.state.clear()
            app.state.update(old)


class NonblockingSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_settlement_does_not_block_live_cycles(self):
        calls={"market":0,"settle":0}
        blocker=asyncio.Event()
        old_poll=app.POLL

        async def market(client):
            calls["market"]+=1
            return None,"test_no_current_contract"

        async def blocked_settle(client):
            calls["settle"]+=1
            await blocker.wait()

        intel={"trade_flow":{"status":"unavailable","exchanges":[]},
               "news":{},"order_book":{}}
        app.POLL=0.01
        try:
            with (patch("app.setup"),
                  patch("app.market_15m",new=market),
                  patch("app.spot_data",new=AsyncMock(return_value={"price":None})),
                  patch("app.check_auth",new=AsyncMock(return_value={})),
                  patch("app.settle",new=blocked_settle),
                  patch("app.confirm",new=AsyncMock(return_value={})),
                  patch("app.public_intelligence",return_value=intel),
                  patch("app.health_checks",return_value=[]),
                  patch("app.persist_alerts")):
                task=asyncio.create_task(app.loop())
                try:
                    # Allow a bounded startup window on loaded CI runners.
                    for _ in range(100):
                        if calls["market"] >= 3 and calls["settle"] >= 1:
                            break
                        await asyncio.sleep(0.01)
                    self.assertGreaterEqual(calls["market"],3)
                    self.assertEqual(calls["settle"],1, {"status":app.state.get("status"),"error":app.state.get("error"),"market":calls["market"],"cycle":app.state.get("last_cycle_duration_seconds")})
                finally:
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
                    blocker.set()
        finally:
            app.POLL=old_poll


    async def test_settlement_failure_is_reported_without_stopping_live_cycles(self):
        calls={"market":0,"settle":0}
        old_poll=app.POLL
        old_state=dict(app.state)

        async def market(client):
            calls["market"]+=1
            return None,"test_no_current_contract"

        async def failed_settle(client):
            calls["settle"]+=1
            raise RuntimeError("settlement endpoint temporarily unavailable")

        intel={"trade_flow":{"status":"unavailable","exchanges":[]},
               "news":{},"order_book":{}}
        app.POLL=0.01
        app.state["settlement_task_error"]=None
        try:
            with (patch("app.setup"),
                  patch("app.market_15m",new=market),
                  patch("app.spot_data",new=AsyncMock(return_value={"price":None})),
                  patch("app.check_auth",new=AsyncMock(return_value={})),
                  patch("app.settle",new=failed_settle),
                  patch("app.confirm",new=AsyncMock(return_value={})),
                  patch("app.public_intelligence",return_value=intel),
                  patch("app.health_checks",return_value=[]),
                  patch("app.persist_alerts")):
                task=asyncio.create_task(app.loop())
                try:
                    for _ in range(100):
                        if calls["market"] >= 3 and app.state.get("settlement_task_error"):
                            break
                        await asyncio.sleep(0.01)
                    self.assertGreaterEqual(calls["market"],3)
                    self.assertEqual(calls["settle"],1)
                    self.assertIn("RuntimeError",app.state.get("settlement_task_error") or "")
                finally:
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
        finally:
            app.POLL=old_poll
            app.state.clear()
            app.state.update(old_state)


class DiagnosticCoverageTests(unittest.TestCase):
    def test_read_only_audit_includes_five_minute_and_quick_samples(self):
        from monitor_health import diagnostic_extras
        five={"accuracy":{"wins":7,"losses":3,"graded":10,"accuracy_pct":70.0}}
        state={"feed_health":{"status":"FRESH","age_seconds":4},
               "settlement_diagnostic":{"overdue_markets":0},
               "settlement_task_error":None,"quick_accuracy_error":None}
        snapshot=diagnostic_extras(state,five,lambda:{"graded":20,"accuracy_pct":55.0})
        self.assertEqual(snapshot["five_minute_accuracy"]["graded"],10)
        self.assertEqual(snapshot["quick_accuracy"]["graded"],20)
        self.assertEqual(snapshot["feed_health"]["status"],"FRESH")
        self.assertEqual(snapshot["settlement_diagnostic"]["overdue_markets"],0)

    def test_failed_quick_audit_does_not_hide_five_minute_or_worker_error(self):
        from monitor_health import diagnostic_extras
        def broken():
            raise RuntimeError("database temporarily unavailable")
        snapshot=diagnostic_extras({"settlement_task_error":"timeout"},
                                   {"accuracy":{"graded":3,"wins":2}},broken)
        self.assertEqual(snapshot["five_minute_accuracy"]["graded"],3)
        self.assertEqual(snapshot["quick_accuracy"]["status"],"unavailable")
        self.assertEqual(snapshot["quick_accuracy"]["reason"],"RuntimeError")
        self.assertEqual(snapshot["settlement_task_error"],"timeout")


if __name__=="__main__":
    unittest.main()
