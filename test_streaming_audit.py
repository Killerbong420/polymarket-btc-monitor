"""Bounded settlement audit cursor and unchanged paired comparison regression tests."""
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch
import app


class StreamingAuditTests(unittest.TestCase):
    def test_cursor_reads_bounded_batches_without_fetchall(self):
        class Cursor:
            def __init__(self):
                self.values = [(i,) for i in range(7)]
                self.fetch_sizes = []
                self.closed = False
                self.sql = None
            def execute(self, sql, params):
                self.sql, self.params = sql, params
            def fetchmany(self, size):
                self.fetch_sizes.append(size)
                batch, self.values = self.values[:size], self.values[size:]
                return batch
            def fetchall(self):
                raise AssertionError("Unbounded fetchall is forbidden")
            def close(self):
                self.closed = True

        cursor = Cursor()
        class Connection:
            def cursor(self):
                return cursor
        @contextmanager
        def fake_connect():
            yield Connection()

        with patch.object(app, "connect", fake_connect), patch.object(app, "DB", ""):
            rows = list(app.iter_query_rows("SELECT value FROM audit WHERE x=?", (42,), batch_size=3))
        self.assertEqual(rows, [(i,) for i in range(7)])
        self.assertEqual(cursor.fetch_sizes, [3, 3, 3, 3])
        self.assertEqual(cursor.params, (42,))
        self.assertTrue(cursor.closed)

    def test_postgres_uses_server_side_named_cursor(self):
        class ServerCursor:
            def __init__(self):
                self.closed=False
                self.rows=[("ok",)]
            def execute(self, sql, params):
                self.sql,self.params=sql,params
            def fetchmany(self, size):
                batch,self.rows=self.rows[:size],self.rows[size:]
                return batch
            def close(self):
                self.closed=True
        cursor=ServerCursor()
        names=[]
        class Connection:
            def cursor(self, name=None):
                names.append(name)
                return cursor
        @contextmanager
        def fake_connect():
            yield Connection()
        with patch.object(app, "connect", fake_connect), patch.object(app, "DB", "postgres://test"):
            rows=list(app.iter_query_rows("SELECT value FROM audit WHERE x=?", (7,), batch_size=2))
        self.assertEqual(rows, [("ok",)])
        self.assertEqual(cursor.sql, "SELECT value FROM audit WHERE x=%s")
        self.assertEqual(cursor.params, (7,))
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("btc_audit_"))
        self.assertTrue(cursor.closed)

    def test_research_rows_use_bounded_stream_instead_of_fetchall(self):
        records=[("market", "EARLY", "now", "{}", "{}", None)]
        with patch.object(app, "iter_query_rows", return_value=iter(records)) as stream, \
             patch.object(app, "execute", side_effect=AssertionError("unbounded query")):
            self.assertEqual(list(app.research_rows()), records)
        stream.assert_called_once()
        self.assertIn("LIMIT 10000", stream.call_args.args[0])

    def test_outcome_feedback_streams_two_independent_filtered_passes(self):
        calls=json.dumps({"AI":{"source":"AI_REVIEW","direction":"UP","confidence":"MIXED"}})
        rows=[
            ("old", "EARLY", "now", "{}", calls, "UP"),
            ("current", "EARLY", "now", "{}", calls, "DOWN"),
            ("pending", "EARLY", "now", "{}", calls, None),
        ]
        app._outcome_feedback_cache.clear()
        try:
            with patch.object(app, "iter_query_rows", side_effect=lambda *a, **kw: iter(rows)) as stream, \
                 patch.object(app, "execute", side_effect=AssertionError("unbounded query")):
                report=app.outcome_feedback("current")
            self.assertEqual(stream.call_count, 2)
            self.assertEqual(report["excluded_market"], "current")
            self.assertEqual(report["strategies"][0]["wins"], 1)
            self.assertEqual(report["strategies"][0]["losses"], 0)
            self.assertEqual(report["loss_review"]["cohorts"][0]["graded"], 1)
        finally:
            app._outcome_feedback_cache.clear()

    def test_invalid_batch_size_rejected(self):
        with self.assertRaises(ValueError):
            list(app.iter_query_rows("SELECT 1", batch_size=0))

    def test_numeric_audit_keeps_paired_accuracy_and_official_settlement(self):
        snapshot = {"settlement_forecast": {"current_reference_usd": 100.0}}
        calls = {
            "AI": {"source": "AI_REVIEW", "direction": "UP", "forecast_final_mean_usd": 102.0},
            "PRICE_PATH": {"direction": "UP", "forecast_final_mean_usd": 101.0},
        }
        rows = [("HALFWAY", json.dumps(snapshot), json.dumps(calls), 101.5, "UP")]
        with patch.object(app, "iter_query_rows", return_value=iter(rows)) as stream:
            report = app.numeric_price_forecast_report()
        stream.assert_called_once()
        self.assertEqual(report["status"], "observed")
        paired = report["paired_comparisons"][0]
        self.assertEqual(paired["paired_rounds"], 1)
        self.assertEqual(paired["ai_mae_usd"], 0.5)
        self.assertEqual(paired["price_path_mae_usd"], 0.5)
        self.assertEqual(report["paired_direction_comparisons"][0]["ai_accuracy_pct"], 100.0)
        self.assertEqual(report["paired_direction_comparisons"][0]["path_accuracy_pct"], 100.0)


if __name__ == "__main__":
    unittest.main()
