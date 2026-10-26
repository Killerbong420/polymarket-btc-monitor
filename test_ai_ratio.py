"""Win/loss rate must count genuine AI predictions, not fallback signals."""
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
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

    def insert_ai(self, index, result, source="AI_REVIEW", direction="UP", created_at="2026-10-26T19:00:00Z"):
        app.execute(
            """INSERT INTO ai_decisions (id,market,phase,direction,confidence,source,reason,created_at,result)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (str(index), "btc-updown-15m-test-"+str(index), "FINAL_15M", direction,
             "HIGH", source, "test", created_at, result))

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

    def test_rolling_100_settled_without_deleting_history(self):
        start=datetime(2026,10,25,tzinfo=timezone.utc)
        # 20 old wins are deliberately outside the newest-100 scoreboard.
        for i in range(1,121):
            result="WIN" if i<=20 or i>70 else "LOSS"
            self.insert_ai(i, result, created_at=(start+timedelta(minutes=i)).isoformat())
        self.insert_ai(121,"PENDING",created_at=(start+timedelta(minutes=121)).isoformat())
        self.insert_ai(122,"LOSS",source="INDICATORS_FALLBACK",
                       created_at=(start+timedelta(minutes=122)).isoformat())
        p=TestClient(app.app).get("/api/status").json()["ai_performance"]
        self.assertEqual(p["window_size"],100)
        self.assertEqual(p["graded"],100)
        self.assertEqual((p["wins"],p["losses"]),(50,50))
        self.assertEqual(p["accuracy_pct"],50.0)
        self.assertEqual(p["pending"],1)
        # Even though the board is capped, all original records survive.
        self.assertEqual(app.execute("SELECT COUNT(*) FROM ai_decisions",fetch=True)[0][0],122)

        self.insert_ai(123,"WIN",created_at=(start+timedelta(minutes=123)).isoformat())
        payload=TestClient(app.app).get("/api/status").json()
        p=payload["ai_performance"]
        self.assertEqual((p["wins"],p["losses"]),(51,49))
        self.assertEqual(p["graded"],100)
        self.assertEqual(p["accuracy_pct"],51.0)
        self.assertEqual(p["pending"],1)
        self.assertEqual(payload["ai_decision_counts"],{"WIN":51,"LOSS":49,"PENDING":1})
        self.assertEqual(app.execute("SELECT COUNT(*) FROM ai_decisions",fetch=True)[0][0],123)

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
