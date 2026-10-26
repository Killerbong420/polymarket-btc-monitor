"""Win/loss rate must count genuine AI predictions, not fallback signals."""
import os
import tempfile
import unittest
from fastapi.testclient import TestClient
import app


class AiAccuracyTests(unittest.TestCase):
    def setUp(self):
        self.prev = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.prev_db = app.DB
        app.DB = ""
        app.setup()

    def tearDown(self):
        app.DB = self.prev_db
        os.chdir(self.prev)
        self.temp.cleanup()

    def insert_ai(self, index, result, source="AI_REVIEW", direction="UP"):
        app.execute(
            """INSERT INTO ai_decisions (id,market,phase,direction,confidence,source,reason,created_at,result)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(index), "btc-updown-15m-test-"+str(index), "FINAL_15M", direction,
             "HIGH", source, "test", "2026-10-26T19:00:00Z", result))

    def test_single_source_and_ratio(self):
        self.insert_ai(1, "WIN")
        self.insert_ai(2, "WIN")
        self.insert_ai(3, "LOSS")
        self.insert_ai(4, "PENDING")
        self.insert_ai(5, "WIN", source="INDICATORS_FALLBACK")
        result=TestClient(app.app).get("/api/status")
        self.assertEqual(result.status_code, 200)
        payload=result.json()
        p=payload["ai_performance"]
        self.assertEqual(p["wins"], 2)
        self.assertEqual(p["losses"], 1)
        self.assertEqual(p["pending"], 1)
        self.assertEqual(p["graded"], 3)
        self.assertEqual(p["accuracy_pct"], 66.7)
        self.assertEqual(p["win_loss_ratio"], 2.0)
        self.assertEqual(p["ratio_label"], "2:1")
        self.assertEqual(payload["ai_verified_counts"], payload["ai_decision_counts"])

    def test_no_settlements_yet(self):
        self.insert_ai(1, "PENDING")
        p=TestClient(app.app).get("/api/status").json()["ai_performance"]
        self.assertIsNone(p["accuracy_pct"])
        self.assertIsNone(p["win_loss_ratio"])
        self.assertEqual(p["pending"], 1)
        self.assertEqual(p["graded"], 0)

    def test_no_losses_yet(self):
        self.insert_ai(1, "WIN")
        self.insert_ai(2, "WIN")
        p=TestClient(app.app).get("/api/status").json()["ai_performance"]
        self.assertEqual(p["accuracy_pct"], 100.0)
        self.assertIsNone(p["win_loss_ratio"])
        self.assertEqual(p["ratio_label"], "2:0")


if __name__=="__main__":
    unittest.main()
