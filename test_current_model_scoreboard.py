"""Current-model scoreboard must never silently display archived AI accuracy."""
import unittest
from unittest.mock import patch
import app


class CurrentModelScoreboardTests(unittest.TestCase):
    def setUp(self):
        self.original=dict(app._status_cache)
        app._status_cache.update(at=0.0,data=None,execute_id=None,write_version=None)

    def tearDown(self):
        app._status_cache.clear()
        app._status_cache.update(self.original)

    def test_rule_based_scoreboard_excludes_old_ai_and_pending(self):
        queried=[]
        def execute(sql,params=(),fetch=False):
            queried.append(sql)
            if "SELECT result,COUNT(*) FROM signals" in sql:
                return [("WIN",2),("LOSS",1)]
            if "SELECT market,phase,direction,flip,score" in sql:
                return []
            if "SELECT market,phase,direction,confidence" in sql:
                return []
            if "SELECT result FROM ai_decisions" in sql:
                return [("WIN",)]*9
            if "COUNT(*) FROM ai_decisions" in sql:
                return [(4,)]
            if "SELECT result FROM signals" in sql and "decision_source='RULE_BASED'" in sql:
                return [("WIN",),("LOSS",),("WIN",)]
            if "COUNT(*) FROM signals" in sql and "decision_source='RULE_BASED'" in sql:
                return [(2,)]
            raise AssertionError("Unexpected query: "+sql)
        with patch.object(app,"execute",side_effect=execute), patch.object(app,"accuracy_report",return_value={}):
            data=app._read_status_history()
        self.assertEqual(data["model_performance"]["source"],"RULE_BASED")
        self.assertEqual(data["model_performance"]["wins"],2)
        self.assertEqual(data["model_performance"]["losses"],1)
        self.assertEqual(data["model_performance"]["pending"],2)
        self.assertEqual(data["model_performance"]["graded"],3)
        self.assertAlmostEqual(data["model_performance"]["accuracy_pct"],66.7)
        self.assertEqual(data["model_counts"],{"WIN":2,"LOSS":1,"PENDING":2})
        # Historical AI data remains separately archived and must not populate
        # the visible current-model headline or its round tiles.
        self.assertEqual(data["ai_performance"]["wins"],9)
        self.assertTrue(any("decision_source='RULE_BASED'" in sql for sql in queried))


if __name__=="__main__":
    unittest.main()
