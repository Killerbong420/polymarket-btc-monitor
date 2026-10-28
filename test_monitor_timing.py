"""Network-delay, round-identity, and five-minute worker regressions."""
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import app
import research
import five_min_monitor as five
from test_five_min_monitor import MemoryDB


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class ReferenceTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_delay_counts_towards_tick_age(self):
        slug = 'btc-updown-15m-2026-10-28-1200z'
        now = research.market_start(slug) + 100
        body = {'series': [{'asset': {'symbol': 'btc', 'assetClass': 'ASSET_CLASS_CRYPTO'},
                           'currency': 'USD', 'candles': [
                               {'timestamp': now-14, 'close': 100, 'tickCount': 1},
                               {'timestamp': now-13.5, 'close': 999, 'tickCount': 1}]}]}
        client = type('Client', (), {'get': AsyncMock(return_value=Response(body))})()
        with patch('research.time.time', side_effect=[now, now+5]):
            result = await research.reference_prices(client, {'slug': slug, 'assetPriceTerms': {'indexSymbol': 'BRTI'}})
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['age_seconds'], 19)
        self.assertEqual(result['price'], 100)

    async def test_market_lookup_rejects_another_round(self):
        now = datetime(2026, 10, 28, 12, 1, tzinfo=timezone.utc)
        client = type('Client', (), {'get': AsyncMock(return_value=Response({
            'markets': [{'slug': 'btc-updown-15m-2026-10-28-1145z'}]}))})()
        with patch('datetime.datetime') as clock:
            clock.now.return_value = now
            market, _ = await app.market_15m(client)
        self.assertIsNone(market)


class FiveMinuteTimingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = MemoryDB()
        self.old_state, self.old_market = dict(five.state), five._last_market
        five.state.clear()
        five._last_market = None

    async def asyncTearDown(self):
        five.state.clear()
        five.state.update(self.old_state)
        five._last_market = self.old_market
        self.db.close()

    async def test_blocked_settlement_does_not_stop_live_updates_and_is_awaited_on_shutdown(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        calls = 0

        async def discover(_):
            nonlocal calls
            calls += 1
            return None, 'no market'

        async def blocked(*_):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with (patch.object(five, 'discover', new=discover),
              patch.object(five, 'score_pending', new=blocked),
              patch.object(five, 'POLL_SECONDS', .001)):
            task = asyncio.create_task(five.loop(None, self.db.execute, None, {}))
            try:
                await asyncio.wait_for(started.wait(), 1)
                for _ in range(100):
                    if calls >= 3:
                        break
                    await asyncio.sleep(.005)
                self.assertGreaterEqual(calls, 3)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(stopped.is_set())

    async def run_cycle(self, market, clock, reference):
        async def stop(_):
            raise asyncio.CancelledError
        with (patch.object(five, 'discover', new=AsyncMock(return_value=(market, None))),
              patch.object(five, 'reference_prices', new=reference),
              patch.object(five.time, 'time', side_effect=lambda: clock[0]),
              patch.object(five.asyncio, 'sleep', new=stop)):
            with self.assertRaises(asyncio.CancelledError):
                await five.live_loop(None, self.db.execute, {})

    async def test_delayed_reference_cannot_create_call_after_cutoff(self):
        clock = [1000]
        market = {'slug': 'test', 'end': 1025,
                  'assetPriceTerms': {'indexSymbol': 'BRTI', 'priceToBeat': 100}}
        async def reference(*_):
            clock[0] += 10
            return {'status': 'fresh'}
        with patch.object(five, 'forecast_candidate') as forecast:
            await self.run_cycle(market, clock, reference)
        forecast.assert_not_called()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM five_min_predictions', fetch=True)[0][0], 0)

    async def test_incompatible_terms_cannot_fall_through_failed_detail_lookup(self):
        market = {'slug': 'test', 'end': 1300, 'assetPriceTerms': {
            'indexSymbol': 'BRTI', 'priceToBeat': 100, 'marketType': 'OTHER'}}
        reference = AsyncMock()
        await self.run_cycle(market, [1000], reference)
        reference.assert_not_awaited()
        self.assertEqual(five.state['phase'], 'UNVERIFIED_TARGET')
        self.assertEqual(five.state['direction'], 'WAIT')


if __name__ == '__main__':
    unittest.main()
