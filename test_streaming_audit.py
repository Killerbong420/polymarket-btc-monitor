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
