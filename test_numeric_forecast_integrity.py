"""Guard against null AI estimates and non-authoritative BTC settlement grading."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import ai_confirmation as ai
import app


def brti_external():
    return {
        "price": 80000.0,
        "reference_price": {
            "status": "fresh",
            "source": "Polymarket US BTC reference / BRTI",
            "price": 80000.0,
        },
    }


def official_market(slug="cpc-btc-updown-15m-2026-10-26-0400z"):
    return {
        "slug": slug,
        "status": "RESOLVED",
        "marketSides": [
            {"long": True, "description": "Yes"},
            {"long": False, "description": "No"},
        ],
        "assetPriceTerms": {
            "marketType": "ASSET_PRICE_MARKET_TYPE_UP_DOWN",
            "indexSymbol": "BRTI",
            "asset": {"symbol": "BTC"},
            "priceToBeat": 79670.0,
            "settlementPrice": 79671.8,
        },
    }


class OfficialNumericPriceTests(unittest.TestCase):
    def test_valid_published_price_is_used(self):
        self.assertEqual(app.official_numeric_price(official_market(), "UP"), 79671.8)

    def test_mismatched_winner_never_becomes_numeric_truth(self):
        self.assertIsNone(app.official_numeric_price(official_market(), "DOWN"))

    def test_rejects_incomplete_or_non_brtI_metadata(self):
        for field, bad in [
            ("indexSymbol", "BTCUSD"), ("settlementPrice", None),
            ("priceToBeat", None), ("settlementPrice", float("nan")),
        ]:
            market = official_market()
            market["assetPriceTerms"][field] = bad
            with self.subTest(field=field, bad=bad):
                self.assertIsNone(app.official_numeric_price(market, "UP"))
        pending = official_market()
        pending["status"] = "OPEN"
        self.assertIsNone(app.official_numeric_price(pending, "UP"))

    def test_numeric_report_excludes_unverified_legacy_prices(self):
        queries = []
        def fake_execute(query, *args, **kwargs):
            queries.append(query)
            return []
        with patch("app.execute", side_effect=fake_execute):
            report = app.numeric_price_forecast_report()
        self.assertIn("OFFICIAL_MARKET_SETTLEMENT", queries[0])
        self.assertEqual(report["status"], "awaiting_official_settlement_prices")


class OfficialNumericBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_backfill_replaces_legacy_reconstructed_price(self):
        market = official_market()
        client = type("Client", (), {})()
        response = type("Response", (), {})()
        response.json = lambda: {"event": {"markets": [market]}}
        response.raise_for_status = lambda: None
        client.get = AsyncMock(return_value=response)
        updates = []
        def fake_execute(query, params=(), fetch=False):
            if fetch: return [(market["slug"], "UP")]
            updates.append((query, params))
        with (patch("app.execute", side_effect=fake_execute),
              patch("app.market_start", return_value=1000)):
            app._reference_retry_after.clear()
            result = await app.retry_numeric_final_means(client, now=3000)
        self.assertEqual(result["graded"], 1)
        self.assertEqual(result["requested"], 1)
        self.assertIn("OFFICIAL_MARKET_SETTLEMENT", updates[0][0])
        self.assertEqual(updates[0][1][0], 79671.8)

    async def test_backfill_rejects_inconsistent_metadata(self):
        market = official_market()
        market["assetPriceTerms"]["settlementPrice"] = 79660
        client = type("Client", (), {})()
        response = type("Response", (), {})()
        response.json = lambda: {"event": {"markets": [market]}}
        response.raise_for_status = lambda: None
        client.get = AsyncMock(return_value=response)
        updates = []
        def fake_execute(query, params=(), fetch=False):
            if fetch: return [(market["slug"], "UP")]
            updates.append(query)
        with (patch("app.execute", side_effect=fake_execute),
              patch("app.market_start", return_value=1000)):
            app._reference_retry_after.clear()
            result = await app.retry_numeric_final_means(client, now=3000)
            app._reference_retry_after.clear()
        self.assertEqual(result["incomplete"], 1)
        self.assertEqual(updates, [])


class AiRequiredNumericForecastTests(unittest.IsolatedAsyncioTestCase):
    def test_require_numeric_only_with_healthy_official_input(self):
        ext = brti_external()
        self.assertEqual(ai.numeric_forecast_schema(ext, 80005), {"type": "number"})
        stale = brti_external()
        stale["reference_price"]["status"] = "stale"
        self.assertEqual(ai.numeric_forecast_schema(stale, 80005), {"type": ["number", "null"]})
        self.assertEqual(ai.numeric_forecast_schema(ext, None), {"type": ["number", "null"]})
        self.assertEqual(ai.numeric_forecast_schema(ext, float("nan")), {"type": ["number", "null"]})

    async def test_live_ai_request_enforces_numeric_in_actual_schema(self):
        output = {"verdict": "UP", "reason": "Futures trend rising", "flip_risk": "LOW",
                  "forecast_final_mean_usd": 80020, "price_path_reason": "Forecast higher",
                  "confidence": "LOW", "news_ids": [], "news_effect": "NONE"}
        response = type("Response", (), {})()
        response.json = lambda: {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}]}
        response.raise_for_status = lambda: None
        client = type("Client", (), {})()
        client.post = AsyncMock(return_value=response)
        with (patch.dict("os.environ", {"OPENAI_API_KEY": "test"}, clear=False),
              patch.object(ai, "_last_request", -100000000)):
            result = await ai.confirm(client, "cpc-btc-updown-15m-2026-10-26-0400z",
                "EARLY", "UP", "WAIT", 1.0, brti_external(), price_to_beat=80005, seconds_left=600)
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["text"]["format"]["schema"]["properties"]
                         ["forecast_final_mean_usd"], {"type": "number"})
        self.assertTrue(json.loads(payload["input"])["numeric_forecast_required"])
        self.assertEqual(result["forecast_final_mean_usd"], 80020.0)

    async def test_checkpoint_request_enforces_numeric_in_actual_schema(self):
        output = {"direction": "UP", "reason": "Expected later move", "confidence": "LOW",
                  "news_ids": [], "news_effect": "NONE",
                  "forecast_final_mean_usd": 80020, "price_path_reason": "Likely continuation"}
        response = type("Response", (), {})()
        response.json = lambda: {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(output)}]}]}
        response.raise_for_status = lambda: None
        client = type("Client", (), {})()
        client.post = AsyncMock(return_value=response)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test"}, clear=False):
            result = await ai.halfway_decision(client,
                "cpc-btc-updown-15m-2026-10-26-0400z", 15, "UP",
                1.0, brti_external(), price_to_beat=80005)
        payload = client.post.await_args.kwargs["json"]
        self.assertEqual(payload["text"]["format"]["schema"]["properties"]
                         ["forecast_final_mean_usd"], {"type": "number"})
        self.assertTrue(json.loads(payload["input"])["numeric_forecast_required"])
        self.assertEqual(result["forecast_final_mean_usd"], 80020.0)


if __name__ == "__main__":
    unittest.main()
