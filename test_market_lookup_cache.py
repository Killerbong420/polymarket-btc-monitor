"""Verified contract metadata may be reused briefly, never past its round or target validity."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import app


class FakeResponse:
    def __init__(self, data=None, status=200):
        self.data = data or {}
        self.status_code = status

    def json(self):
        return self.data


class MarketLookupCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = dict(app._market_15m_cache)
        app._market_15m_cache.clear()
        app._market_15m_cache.update(event_slug=None, market=None, checked_monotonic=0.0)
        self.now = datetime(2026, 11, 8, 12, 1, tzinfo=timezone.utc)
        self.mono = 100.0
        self.date_patch = patch.object(app, "datetime")
        self.time_patch = patch.object(app, "time")
        self.fake_date = self.date_patch.start()
        self.fake_time = self.time_patch.start()
        self.fake_date.now.side_effect = lambda tz=None: self.now
        self.fake_time.time.side_effect = lambda: self.now.timestamp()
        self.fake_time.monotonic.side_effect = lambda: self.mono

    async def asyncTearDown(self):
        self.time_patch.stop()
        self.date_patch.stop()
        app._market_15m_cache.clear()
        app._market_15m_cache.update(self.saved)

    def event(self, price=75000, minutes=0):
        date = self.now.replace(minute=(self.now.minute // 15) * 15,
                                second=0, microsecond=0) + timedelta(minutes=minutes)
        slug = "btc-updown-15m-" + date.strftime("%Y-%m-%d-%H%Mz").lower()
        return {"markets": [{"slug": "cpc-" + slug, "assetPriceTerms": {
            "indexSymbol": "BRTI", "priceToBeat": {"value": price}}}]}

    async def test_reuses_verified_contract_without_duplicate_network_fetch(self):
        client = type("Client", (), {"get": AsyncMock(return_value=FakeResponse(self.event()))})()
        first, reason = await app.market_15m(client)
        self.assertIsNone(reason)
        self.assertEqual(first["assetPriceTerms"]["priceToBeat"]["value"], 75000)
        self.mono += 5
        second, reason = await app.market_15m(client)
        self.assertIsNone(reason)
        self.assertEqual(second, first)
        self.assertIsNot(second, first)
        self.assertEqual(client.get.await_count, 1)

    async def test_refetches_official_target_after_short_cache_expiry(self):
        client = type("Client", (), {"get": AsyncMock(side_effect=[
            FakeResponse(self.event(price=75000)),
            FakeResponse(self.event(price=75100))
        ])})()
        first, _ = await app.market_15m(client)
        self.mono += app._MARKET_LOOKUP_CACHE_SECONDS + 1
        second, _ = await app.market_15m(client)
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(first["assetPriceTerms"]["priceToBeat"]["value"], 75000)
        self.assertEqual(second["assetPriceTerms"]["priceToBeat"]["value"], 75100)

    async def test_new_round_always_fetches_new_contract(self):
        client = type("Client", (), {"get": AsyncMock(side_effect=[
            FakeResponse(self.event(price=75000)),
            FakeResponse(self.event(price=75200, minutes=15))
        ])})()
        first, _ = await app.market_15m(client)
        self.now += timedelta(minutes=15)
        second, reason = await app.market_15m(client)
        self.assertIsNone(reason)
        self.assertNotEqual(first["slug"], second["slug"])
        self.assertEqual(client.get.await_count, 2)

    async def test_no_official_target_means_no_cache(self):
        unverified = self.event()
        del unverified["markets"][0]["assetPriceTerms"]["priceToBeat"]
        client = type("Client", (), {"get": AsyncMock(side_effect=[
            FakeResponse(unverified), FakeResponse(self.event())
        ])})()
        first, _ = await app.market_15m(client)
        self.assertNotIn("priceToBeat", first["assetPriceTerms"])
        second, _ = await app.market_15m(client)
        self.assertEqual(client.get.await_count, 2)
        self.assertEqual(second["assetPriceTerms"]["priceToBeat"]["value"], 75000)

    async def test_failed_refresh_does_not_use_expired_cache(self):
        client = type("Client", (), {"get": AsyncMock(side_effect=[
            FakeResponse(self.event()), FakeResponse(status=503)
        ])})()
        await app.market_15m(client)
        self.mono += app._MARKET_LOOKUP_CACHE_SECONDS + 1
        market, reason = await app.market_15m(client)
        self.assertIsNone(market)
        self.assertEqual(reason, "event_http_503")
        self.assertEqual(client.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
