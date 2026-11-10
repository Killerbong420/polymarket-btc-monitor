"""Recent-round tiles use real graded records, never pending guesses."""
import sqlite3
import unittest

from round_tiles import TILE_LIMIT, report, fifteen_rounds


class TileHistoryTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.db.execute("""CREATE TABLE ai_decisions (
            id TEXT PRIMARY KEY,market TEXT,phase TEXT,direction TEXT,
            confidence TEXT,source TEXT,reason TEXT,created_at TEXT,result TEXT)""")

    def tearDown(self):
        self.db.close()

    def execute(self, sql, params=(), fetch=False):
        cur=self.db.execute(sql,params)
        return cur.fetchall() if fetch else None

    def ai(self, i, side="UP", result="WIN", source="AI_REVIEW", phase="FINAL_15M", market=None):
        market=market or f"cpc-btc-updown-15m-2026-10-28-{i:04d}z"
        self.execute("INSERT INTO ai_decisions VALUES (?,?,?,?,?,?,?,?,?)",
                     (f"a{i}",market,phase,side,"HIGH",source,"",f"2026-10-28T{i//60:02d}:{i%60:02d}:00Z",result))

    def test_15m_outcomes_reflect_verified_ai_result_not_current_side(self):
        self.ai(1,"UP","LOSS")
        self.ai(2,"DOWN","WIN")
        self.ai(3,"UP","PENDING")
        self.ai(4,"DOWN","WIN","INDICATORS")
        self.ai(5,"UP","WIN","AI_REVIEW","EARLY")
        r=fifteen_rounds(self.execute)
        self.assertEqual(r["summary"],{"rounds":2,"up":0,"down":2,"scored":2,
                                         "correct":1,"incorrect":1,"accuracy_pct":50})
        self.assertEqual([x["winner"] for x in r["rounds"]],["DOWN","DOWN"])
        self.assertEqual([x["correct"] for x in r["rounds"]],[False,True])
        self.assertEqual([x["prediction"] for x in r["rounds"]],["UP","DOWN"])

    def test_ai_rounds_deduplicate_same_market_and_limit_to_36(self):
        for i in range(1,45):
            self.ai(i,"UP","WIN")
        # A duplicate later AI row must not become an extra round.
        self.ai(46,"DOWN","LOSS",market="cpc-btc-updown-15m-2026-10-28-0044z")
        r=fifteen_rounds(self.execute)
        self.assertEqual(len(r["rounds"]),TILE_LIMIT)
        self.assertEqual(r["summary"]["rounds"],TILE_LIMIT)
        self.assertEqual(len(set(x["market"] for x in r["rounds"])),TILE_LIMIT)
        self.assertTrue(r["rounds"][-1]["captured_at"]>r["rounds"][0]["captured_at"])

    def test_endpoint_report_is_compact_and_has_no_contract_value(self):
        self.ai(1,"UP","WIN")
        data=report(self.execute)
        self.assertEqual(data["limit"],36)
        self.assertEqual(data["ordering"],"oldest_to_newest")
        self.assertEqual(data["fifteen"]["summary"]["correct"],1)
        self.assertNotIn("five",data)
        self.assertNotIn("contract_value",str(data))
        self.assertNotIn("profit",str(data).lower())


if __name__=="__main__":
    unittest.main()
