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
                    self.assertEqual(calls["settle"],1)
                finally:
                    task.cancel()
                    try: await task
                    except asyncio.CancelledError: pass
                    blocker.set()
        finally:
            app.POLL=old_poll


if __name__=="__main__":
    unittest.main()
