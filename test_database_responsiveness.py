"""Slow database I/O must not stall HTTP requests and the one-second sampler."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import app
import five_min_monitor


class DatabaseResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def assert_loop_responsive(self, operation):
        ticks = []

        async def heartbeat():
            for _ in range(8):
                started = time.monotonic()
                await asyncio.sleep(0.01)
                ticks.append(time.monotonic() - started)

        await asyncio.gather(heartbeat(), operation)
        self.assertLess(max(ticks), 0.15, "Database work blocked the live event loop")

    async def test_settlement_database_reads_do_not_block_live_loop(self):
        def slow_database(*args, **kwargs):
            time.sleep(0.2)
            return []

        with patch.object(app, 'execute', side_effect=slow_database):
            await self.assert_loop_responsive(app.settle(AsyncMock()))

    async def test_five_minute_report_does_not_block_live_loop(self):
        original = dict(five_min_monitor.state)

        def slow_report(execute):
            time.sleep(0.2)
            return {}

        async def run_once():
            task = asyncio.create_task(five_min_monitor.live_loop(AsyncMock(), lambda *a: [], {}))
            try:
                await asyncio.sleep(0.3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            with patch.object(five_min_monitor, 'discover', AsyncMock(return_value=(None, 'test'))), patch.object(five_min_monitor, 'report', side_effect=slow_report):
                await self.assert_loop_responsive(run_once())
        finally:
            five_min_monitor.state.clear()
            five_min_monitor.state.update(original)

    async def test_historical_learning_never_holds_up_live_prices(self):
        from threading import Event
        entered, release = Event(), Event()
        old_task = app._feedback_task
        old_cache = dict(app._outcome_feedback_cache)
        calls = []

        def slow_learning(market):
            calls.append(market)
            entered.set()
            release.wait(1)
            app._outcome_feedback_cache[market] = (time.time(), {'excluded_market': market})

        app._feedback_task = None
        app._outcome_feedback_cache.clear()
        try:
            with patch.object(app, 'outcome_feedback', side_effect=slow_learning):
                self.assertEqual(app.live_outcome_feedback('current')['status'], 'collecting')
                await asyncio.to_thread(entered.wait, 1)
                first_task = app._feedback_task
                for _ in range(4):
                    self.assertEqual(app.live_outcome_feedback('current')['status'], 'collecting')
                    self.assertIs(app._feedback_task, first_task)
                release.set()
                await first_task
                self.assertEqual(app.live_outcome_feedback('current')['excluded_market'], 'current')
                self.assertEqual(calls, ['current'])
        finally:
            release.set()
            if app._feedback_task:
                await app._feedback_task
            app._feedback_task = old_task
            app._outcome_feedback_cache.clear()
            app._outcome_feedback_cache.update(old_cache)

    async def test_five_minute_http_uses_worker_snapshot_without_sql(self):
        with patch.dict(five_min_monitor.state, {'accuracy': {'wins': 55, 'losses': 45}}), patch.object(app, 'execute', side_effect=AssertionError('HTTP must not query SQL')):
            self.assertEqual(app.five_minute_report()['accuracy']['wins'], 55)
