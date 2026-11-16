import asyncio
import unittest
from unittest.mock import patch

import polymarket_ws_diagnostic as diagnostic


class DiagnosticTests(unittest.TestCase):
    def test_status_is_sanitized_when_credentials_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            asyncio.run(diagnostic.run_once(lambda: "btc-updown-15m-2026-11-16-2030z", wait_seconds=.01))
        result = diagnostic.snapshot()
        self.assertEqual(result["reason"], "missing_credentials")
        self.assertNotIn("secret", result)
        self.assertNotIn("signature", result)

    def test_no_market_fails_closed(self):
        asyncio.run(diagnostic.run_once(lambda: None, wait_seconds=.01))
        self.assertEqual(diagnostic.snapshot()["reason"], "market_unavailable")


if __name__ == "__main__":
    unittest.main()
