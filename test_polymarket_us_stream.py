import unittest
from polymarket_us_stream import QuoteCache, parse_quote, subscription, auth_headers


class PolymarketUSStreamTests(unittest.TestCase):
    SLUG = "btc-updown-15m-2026-11-16-2000z"

    def test_subscribe_lite_and_deduplicate(self):
        data = subscription([self.SLUG, self.SLUG])
        self.assertEqual(data["subscribe"]["marketSlugs"], [self.SLUG])
        self.assertEqual(data["subscribe"]["subscriptionType"], "SUBSCRIPTION_TYPE_MARKET_DATA_LITE")

    def test_reject_non_btc(self):
        with self.assertRaises(ValueError):
            subscription(["unrelated-market"])

    def test_quote_scope_and_crossed_book(self):
        payload = {"marketDataLite": {"marketSlug": self.SLUG,
                    "bestBid": {"value": "0.48", "currency": "USD"},
                    "bestAsk": {"value": "0.52", "currency": "USD"}}}
        self.assertIsNone(parse_quote(payload, ["other"]))
        quote = parse_quote(payload, [self.SLUG], received_at=100)
        self.assertEqual(quote["bid"], 0.48)
        self.assertEqual(quote["ask"], 0.52)
        payload["marketDataLite"]["bestBid"]["value"] = "0.53"
        self.assertIsNone(parse_quote(payload, [self.SLUG]))
        payload["marketDataLite"]["bestBid"]["value"] = "NaN"
        self.assertIsNone(parse_quote(payload, [self.SLUG]))

    def test_cache_expires_and_reconnect_clears(self):
        cache = QuoteCache()
        payload = {"marketDataLite": {"marketSlug": self.SLUG, "bestBid": "0.4", "bestAsk": "0.6"}}
        cache.update(payload, [self.SLUG], received_at=100)
        self.assertIsNotNone(cache.get(self.SLUG, now=110))
        self.assertIsNone(cache.get(self.SLUG, now=111))
        self.assertIsNone(cache.get(self.SLUG, now=99))
        cache.clear()
        self.assertIsNone(cache.get(self.SLUG, now=100))

    def test_auth_requires_credentials(self):
        with self.assertRaises(ValueError):
            auth_headers("", "")


if __name__ == "__main__":
    unittest.main()
