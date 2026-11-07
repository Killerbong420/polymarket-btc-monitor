"""Regression guards for the live dashboard's nonblocking status heartbeat."""
import unittest
from pathlib import Path
from unittest.mock import patch
import app


class StatusHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.saved=dict(app._status_cache)
        app._status_cache.clear()
        app._status_cache.update({"at":0,"data":None,"execute_id":None,"write_version":None})

    def tearDown(self):
        app._status_cache.clear()
        app._status_cache.update(self.saved)

    def test_status_never_queries_sql_during_initialization(self):
        with patch.object(app,"execute",side_effect=AssertionError("status blocked on SQL")), \
             patch.object(app,"_read_status_history",side_effect=AssertionError("status blocked on history")):
            result=app.status()
        self.assertIn("status",result)
        self.assertEqual(result["history"],[])
        self.assertFalse(result["status_history_ready"])
        self.assertIn("server_epoch",result)

    def test_cached_history_remains_visible_if_sql_is_stuck(self):
        data={"history":[{"direction":"UP","result":"WIN"}],"ai_verified_counts":{"WIN":12}}
        app._status_cache["data"]=data
        with patch.object(app,"execute",side_effect=AssertionError("status should not access SQL")):
            result=app.status()
        self.assertTrue(result["status_history_ready"])
        self.assertEqual(result["history"],data["history"])
        self.assertEqual(result["ai_verified_counts"]["WIN"],12)

    def test_history_worker_has_single_non_overlapping_refresh(self):
        source=Path(app.__file__).read_text()
        body=source.split("async def _status_history_worker():",1)[1].split('@app.get("/api/status")',1)[0]
        self.assertIn("await asyncio.to_thread(_read_status_history)",body)
        self.assertIn("await asyncio.sleep(15)",body)
        self.assertNotIn("asyncio.create_task(",body)
        self.assertNotIn("asyncio.wait_for(",body)


if __name__=="__main__":
    unittest.main()
