"""Tests for independent numeric forecasts, shadows, and forward-only audits."""
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from ai_confirmation import halfway_decision
from forecast_audit import summarize_forecast_audit
from settlement_forecast import forecast_settlement, regime_shadow_forecast


class Reply:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class IndependentForecastTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_has_no_baseline_prediction_in_prompt_and_ref_validates(self):
        target = 100.3
        decision = {
            "direction": "DOWN", "reason": "Test independent output",
            "confidence": "MEDIUM", "news_ids": [], "news_effect": "NONE",
            "forecast_final_mean_usd": 100.4,
            "price_path_reason": "Observed index drift indicates an eventual rise"
        }
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(decision)}]}]}
        mock = type("Client", (), {"post": AsyncMock(return_value=Reply(response))})()
        external = {
            "price": 100.2,
            "source": "Coinbase Exchange BTC-USD",
            "reference_price": {
                "status": "fresh", "source": "Polymarket US BTC reference / BRTI",
                "price": 100.1
            },
            # Deliberately incompatible baseline: must NOT be leaked to AI or
            # used as the price plausibility validator when BRTI is verified.
            "settlement_forecast": {
                "status": "available",
                "current_reference_usd": 200,
                "forecast_final_mean_usd": 201,
            },
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-nonsecret-placeholder"}):
            result = await halfway_decision(
                mock, "cpc-btc-updown-15m-2026-10-25-1400z",
                15, "DOWN", -1, external, target,
                seconds_left=450, checkpoint="HALFWAY")
        self.assertEqual(result["source"], "AI_REVIEW")
        self.assertEqual(result["direction"], "UP")
        self.assertEqual(result["forecast_final_mean_usd"], 100.4)
        self.assertTrue(result["independent_forecast"])
        self.assertEqual(result["forecast_status"], "NUMERIC_VERIFIED")
        sent = json.loads(mock.post.await_args.kwargs["json"]["input"])
        self.assertNotIn("future_settlement_forecast", sent)
        self.assertTrue(sent["independent_forecast_mode"])

    async def test_direction_only_is_explicit_when_index_is_absent(self):
        decision = {
            "direction": "DOWN", "reason": "Spot candles bearish",
            "confidence": "LOW", "news_ids": [], "news_effect": "NONE",
            "forecast_final_mean_usd": None, "price_path_reason": "Cannot verify index"
        }
        response = {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(decision)}]}]}
        mock = type("Client", (), {"post": AsyncMock(return_value=Reply(response))})()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}):
            result = await halfway_decision(mock, "cpc-btc-updown-15m-2026-10-25-1400z",
                                            15, "UP", 1, {"price": 100.1}, 100.3)
        self.assertEqual(result["direction"], "DOWN")
        self.assertEqual(result["forecast_status"], "DIRECTION_ONLY")
        self.assertIsNone(result["forecast_final_mean_usd"])


class ShadowTests(unittest.TestCase):
    def test_shadow_changes_future_path_without_overriding_live(self):
        end, left, target = 900, 720, 101.0
        now = end-left
        ref = {"status": "fresh", "window_end": end,
               "points": [(t, 100+.01*t) for t in range(now+1)]}
        ext = {"momentum_pct": .2, "candle_age_seconds": 5,
               "external_intelligence": {}, "pressure_available": False}
        base = forecast_settlement(ref,ext,target,left,now=now)
        shadow = regime_shadow_forecast(ref,ext,target,left,now=now)
        self.assertEqual(base["status"],"available")
        self.assertEqual(shadow["status"],"available")
        self.assertEqual(shadow["regime"],"EARLY")
        self.assertLess(abs(shadow["forecast_final_mean_usd"]-base["current_reference_usd"]),
                        abs(base["forecast_final_mean_usd"]-base["current_reference_usd"]))
        # Future observations cannot affect a checkpoint's shadow forecast.
        future = {**ref, "points":ref["points"] + [(t,500) for t in range(now+1,900)]}
        self.assertEqual(shadow["forecast_final_mean_usd"],
                         regime_shadow_forecast(future,ext,target,left,now=now)["forecast_final_mean_usd"])


class AuditTests(unittest.TestCase):
    def row(self, version, ai, target, winner, aprice, pprice, actual,
            model="gpt-test", market="btc-updown-15m-a"):
        snapshot = {"version":version, "ai_model": model,
                    "settlement_forecast":{"current_reference_usd":100}}
        calls = {
            "AI":{"direction":ai, "source":"AI_REVIEW",
                  "forecast_final_mean_usd":aprice,"independent_forecast":version=="v2"},
            "TARGET_SIDE":{"direction":target},
            "PRICE_PATH":{"direction":"DOWN","forecast_final_mean_usd":pprice}
        }
        return (market,"HALFWAY","2026-10-27T00:00:00Z",
                json.dumps(snapshot),json.dumps(calls),winner,actual)

    def test_separates_versions_and_identifies_reversal_value(self):
        old = self.row("v1","UP","UP","UP",101,101,101,market="a")
        new1 = self.row("v2","DOWN","UP","DOWN",99,100,99,market="b")
        new2 = self.row("v2","UP","UP","DOWN",101,100,99,market="c")
        result = summarize_forecast_audit([old,new1,new2])
        self.assertEqual(len(result["groups"]),2)
        byver = {g["version"]:g for g in result["groups"]}
        g=byver["v2"]
        self.assertEqual(g["ai_calls"],2)
        self.assertEqual(g["ai_wins"],1)
        self.assertEqual(g["ai_reverse_calls"],1)
        self.assertEqual(g["successful_reversal_calls"],1)
        self.assertEqual(g["missed_reversals"],1)
        self.assertEqual(g["baseline_wins"],0)
        self.assertEqual(g["paired_numeric_rounds"],2)
        self.assertEqual(g["identical_price_pairs"],0)
        self.assertEqual(g["paired_ai_mae_usd"],1.0)
        self.assertEqual(byver["v1"]["identical_price_pairs"],1)
        self.assertFalse(g["eligible_to_consider_training"])

    def test_rejects_duplicate_market_checkpoint_and_pending_grades(self):
        win=self.row("v2","UP","DOWN","UP",100.4,100.5,100.7,market="a")
        pending=self.row("v2","UP","DOWN",None,100.4,100.5,None,market="b")
        result=summarize_forecast_audit([win,win,pending])
        group=result["groups"][0]
        self.assertEqual(result["ignored_rows"],1)
        self.assertEqual(group["settled_ai_calls"],1)
        self.assertEqual(group["ai_calls"],2)
        self.assertEqual(group["numeric_ai_scored_rounds"],1)


if __name__=="__main__":
    unittest.main()
