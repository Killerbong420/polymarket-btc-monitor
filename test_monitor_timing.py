"""Network-delay and 15-minute round identity regressions."""
import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import app
import research


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


if __name__ == "__main__":
    unittest.main()
