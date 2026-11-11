"""PostgreSQL setup outages must never pin live BTC data to STARTING."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import app


class IndependentDatabaseStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original=dict(app.state)
        app.state.clear()
        app.state.update(status="starting",market=None,signal="WAIT",updated=None,
                         database_ready=False,database_setup_error=None)

    def tearDown(self):
        app.state.clear()
        app.state.update(self.original)

    async def test_retries_postgres_initialization_then_restores_persistence(self):
        attempts=[]
        def start():
            attempts.append("try")
            if len(attempts)<3:
                raise ConnectionError("database not available")
        with patch.object(app,"setup",side_effect=start), patch.object(app,"execute",return_value=[(1,)]):
            worker=asyncio.create_task(app.database_setup_worker(retry_seconds=.005))
            try:
                for _ in range(90):
                    if app.state.get("database_ready"):break
                    await asyncio.sleep(.005)
                self.assertTrue(app.state["database_ready"])
                self.assertIsNone(app.state["database_setup_error"])
                self.assertEqual(len(attempts),3)
            finally:
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker

    async def test_monitor_runs_without_waiting_for_database_schema(self):
        async def sample_spot(_client):
            return {"source":"Coinbase Exchange BTC-USD","price":None,
                    "signal_quality":{"status":"INSUFFICIENT_DATA"}}
        async def missing_round(_client):
            return None,"test_contract_temporarily_unavailable"
        with patch.object(app,"setup",side_effect=AssertionError("do not block on setup")), \
             patch.object(app,"spot_data",side_effect=sample_spot), \
             patch.object(app,"market_15m",side_effect=missing_round), \
             patch.object(app,"public_intelligence",return_value={"trade_flow":{"status":"unavailable","exchanges":[]}}), \
             patch.object(app,"health_checks",return_value=[]), \
             patch.object(app,"persist_alerts",side_effect=AssertionError("offline DB must not be written")), \
             patch.object(app,"execute",side_effect=AssertionError("offline DB must not be read")):
            task=asyncio.create_task(app.loop())
            try:
                for _ in range(70):
                    if app.state.get("status")=="market unavailable":break
                    await asyncio.sleep(.01)
                self.assertEqual(app.state["status"],"market unavailable",app.state.get("error"))
                self.assertEqual(app.state["final_15m"]["lookup"],"test_contract_temporarily_unavailable")
                self.assertFalse(app.state["database_ready"])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    def test_live_api_shows_database_unavailable_without_fabricating_history(self):
        from fastapi.testclient import TestClient
        client=TestClient(app.app)
        out=client.get("/api/live")
        self.assertEqual(out.status_code,200)
        self.assertIn("database_ready",app.state)
        self.assertNotIn("error_connection_string",out.text)
        self.assertNotIn("DATABASE_URL",out.text)


if __name__=="__main__":
    unittest.main()
