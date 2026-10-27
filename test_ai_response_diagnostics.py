"""Diagnostic categories must distinguish AI response failures without leaking content."""
import unittest

from ai_confirmation import ai_failure_category, final_price_direction, parse_structured_response


BRTI = {"reference_price": {
    "status": "fresh", "source": "Polymarket US BTC reference / BRTI",
    "price": 80000.0,
}}


class AiResponseDiagnosticTests(unittest.TestCase):
    def test_valid_structured_output(self):
        payload = {"status": "completed", "output": [{"type": "message",
            "content": [{"type": "output_text", "text": '{"direction":"UP","forecast_final_mean_usd":80050}'}]}]}
        self.assertEqual(parse_structured_response(payload)["direction"], "UP")

    def test_incomplete_max_tokens_has_safe_reason(self):
        payload = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
        with self.assertRaisesRegex(ValueError, "^ai_output_token_limit$"):
            parse_structured_response(payload)

    def test_malformed_json_has_safe_reason(self):
        payload = {"status": "completed", "output": [{"type": "message",
            "content": [{"type": "output_text", "text": '{"forecast_final_mean_usd":'}]}]}
        with self.assertRaisesRegex(ValueError, "^ai_json_invalid$"):
            parse_structured_response(payload)

    def test_empty_output_has_safe_reason(self):
        with self.assertRaisesRegex(ValueError, "^ai_output_empty$"):
            parse_structured_response({"status": "completed", "output": []})

    def test_missing_forecast_has_safe_reason(self):
        with self.assertRaisesRegex(ValueError, "^ai_forecast_missing$"):
            final_price_direction({"forecast_final_mean_usd": None}, BRTI, 80010)

    def test_implausible_forecast_has_safe_reason(self):
        with self.assertRaisesRegex(ValueError, "^ai_forecast_out_of_range$"):
            final_price_direction({"forecast_final_mean_usd": 90000}, BRTI, 80010)

    def test_good_numeric_forecast_still_decides_direction(self):
        self.assertEqual(final_price_direction(
            {"forecast_final_mean_usd": 80050}, BRTI, 80010), (80050.0, "UP"))

    def test_exception_messages_not_exposed(self):
        self.assertEqual(ai_failure_category(ValueError("secret response body")),
                         "ai_valueerror")


if __name__ == "__main__":
    unittest.main()
