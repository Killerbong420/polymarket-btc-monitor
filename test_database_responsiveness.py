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
