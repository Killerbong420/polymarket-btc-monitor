"""Verified checkpoint scoreboard should not hammer low-memory PostgreSQL."""
import unittest
from queue import LifoQueue
from threading import BoundedSemaphore
from unittest.mock import patch
import app


class FakePostgresConnection:
    def __init__(self):
        self.closed=False
        self.broken=False
        self.commits=0
        self.rollbacks=0
        self.closes=0

    def commit(self):
        self.commits+=1

    def rollback(self):
        self.rollbacks+=1

    def close(self):
        self.closed=True
        self.closes+=1


class PooledPostgresTests(unittest.TestCase):
    def test_connection_reused_and_bounded_without_new_tls_per_query(self):
        calls=[]
        def make_connection(*args,**kwargs):
            self.assertEqual(kwargs["connect_timeout"],5)
            conn=FakePostgresConnection()
            calls.append(conn)
            return conn
        with patch.object(app,"DB","postgres://test"), \
             patch.object(app,"_pg_slots",BoundedSemaphore(3)), \
             patch.object(app,"_pg_idle",LifoQueue(maxsize=3)), \
             patch.object(app.psycopg,"connect",side_effect=make_connection):
            with app.connect() as first:
                with app.connect() as second:
                    self.assertIsNot(first,second)
                self.assertEqual(first.commits,0)
            with app.connect() as reused:
                self.assertIn(reused,(first,second))
            self.assertEqual(len(calls),2)
            self.assertEqual(sum(c.commits for c in calls),3)
            app._close_idle_pg_connections()
            self.assertEqual(sum(c.closes for c in calls),2)

    def test_exception_rolls_back_and_drops_broken_connection(self):
        calls=[]
        def make_connection(*_args,**_kwargs):
            conn=FakePostgresConnection()
            calls.append(conn)
            return conn
        with patch.object(app,"DB","postgres://test"), \
             patch.object(app,"_pg_slots",BoundedSemaphore(3)), \
             patch.object(app,"_pg_idle",LifoQueue(maxsize=3)), \
             patch.object(app.psycopg,"connect",side_effect=make_connection):
            with self.assertRaises(ValueError):
                with app.connect() as broken:
                    broken.broken=True
                    raise ValueError("bad transaction")
            self.assertEqual(broken.rollbacks,1)
            self.assertEqual(broken.closes,1)
            with app.connect() as next_conn:
                self.assertIsNot(next_conn,broken)
            self.assertEqual(len(calls),2)
            app._close_idle_pg_connections()


class CheckpointCacheTests(unittest.TestCase):
    def setUp(self):
        self.saved=dict(app._checkpoint_display_cache)
        self.old_db_ready=app.state.get("database_ready")
        app._checkpoint_display_cache.update(updated=0,data=None,error=None)

    def tearDown(self):
        app._checkpoint_display_cache.clear()
        app._checkpoint_display_cache.update(self.saved)
        app.state["database_ready"]=self.old_db_ready

    def test_http_scoreboard_never_connects_to_database(self):
        from fastapi.testclient import TestClient
        app.state["database_ready"]=False
        with patch.object(app,"iter_query_rows",side_effect=AssertionError("HTTP touched Postgres")):
            response=TestClient(app.app).get("/api/checkpoint-scoreboard")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()["status"],"collecting")
            self.assertEqual(response.json()["fifteen"],[])

    def test_only_verified_frozen_rows_are_cached_and_retained_stale(self):
        from fastapi.testclient import TestClient
        import json
        record=("btc-updown-15m-test","HALFWAY",
                json.dumps({"PRICE_PATH":{"direction":"UP"}}),"UP")
        app.state["database_ready"]=True
        with patch.object(app,"iter_query_rows",return_value=iter([record])):
            observed=app._refresh_checkpoint_display()
        self.assertEqual(observed["status"],"available")
        self.assertEqual(len(observed["fifteen"]),3)
        self.assertEqual(observed["fifteen"][1]["accuracy_pct"],100.0)
        with patch.object(app,"iter_query_rows",side_effect=AssertionError("No synchronous DB")), \
             patch.object(app,"DB","postgres://test"):
            client=TestClient(app.app)
            self.assertEqual(client.get("/api/checkpoint-scoreboard").json()["status"],"available")
            app.state["database_ready"]=False
            stale=client.get("/api/checkpoint-scoreboard").json()
            self.assertEqual(stale["status"],"stale")
            self.assertEqual(stale["fifteen"][1]["graded"],1)
            self.assertNotIn("winner",stale)

    def test_dashboard_shows_reconnection_not_fabricated_scores(self):
        from pathlib import Path
        html=Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
        self.assertIn("data.status==='collecting'",html)
        self.assertIn("data.status==='stale'",html)
        self.assertIn("database reconnecting",html.lower())
        self.assertIn("setInterval(()=>{if(!document.hidden)refreshScoreboard()},15000)",html)
        self.assertNotIn("setInterval(refreshScoreboard,45000)",html)


if __name__=="__main__":
    unittest.main()
