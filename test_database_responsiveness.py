"""Slow database I/O must not stall HTTP requests and the one-second sampler."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import app


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

