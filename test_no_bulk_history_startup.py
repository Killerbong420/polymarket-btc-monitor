"""Keep the 672-round seven-day backfill off the live 15-minute bot."""
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
import app


class NoBulkHistoryStartupTests(unittest.TestCase):
    def test_live_service_does_not_launch_historical_collector(self):
        source=Path(app.__file__).read_text(encoding="utf-8")
        lifespan=source.split("@asynccontextmanager\nasync def lifespan(app):",1)[1].split("app=FastAPI(lifespan=lifespan)",1)[0]
        self.assertNotIn("collect_15m",source)
        self.assertNotIn("from historical_15m import",source)
        self.assertNotIn("historical_job",lifespan)
        self.assertNotIn("history_task",lifespan.replace("status_history_task",""))
        self.assertNotIn("historical_15m_7d.json",source)
        self.assertNotIn("historical_15m_7d.csv",source)

    def test_archive_endpoints_cannot_trigger_collection_or_load_672_rows(self):
        client=TestClient(app.app)
        status=client.get("/api/historical-15m/status")
        self.assertEqual(status.status_code,200)
        self.assertEqual(status.json()["status"],"disabled")
        results=client.get("/api/historical-15m/results")
        self.assertEqual(results.status_code,200)
        self.assertEqual(results.json()["records"],[])
        csv=client.get("/api/historical-15m/csv")
        self.assertEqual(csv.status_code,410)

    def test_official_15m_accuracy_and_pattern_persistence_remain(self):
        source=Path(app.__file__).read_text(encoding="utf-8")
        self.assertIn("settlement_task=track_prediction(asyncio.create_task(settle(client)))",source)
        self.assertIn("record_completed_bar,execute,slug,context,candle,time.time()",source)
        self.assertIn("status_history_task=asyncio.create_task(_status_history_worker())",source)
        self.assertIn("trade_stream_task=asyncio.create_task(stream_loop())",source)
        self.assertIn("chart_task=asyncio.create_task(official_chart_sampler(state))",source)


if __name__=="__main__":
    unittest.main()
