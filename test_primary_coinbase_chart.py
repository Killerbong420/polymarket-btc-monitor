"""Read-only primary Coinbase formation chart and safe candle response regressions."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class PrimaryCoinbaseChartTests(unittest.TestCase):
    def test_primary_formation_chart_precedes_official_brtI(self):
        html=Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
        self.assertLess(html.index('id="coinbaseProjectionPanel"'),
                        html.index('id="brtiLinePanel"'))
        self.assertIn("COINBASE BTC/USD · VERIFIED 1M",html)
        self.assertIn("Illustrative +15m formation scenario",html)
        self.assertIn("id=\"brtiLineCanvas\"",html)
        self.assertIn("id=\"brtiLineStatus\"",html)
        self.assertIn("'/api/coinbase-formation/candles'",html)
        self.assertNotIn("78% confidence",html.lower())

    def test_candles_are_actual_completed_coinbase_bars_not_synthesized(self):
        latest=(int(time.time())//60-1)*60
        bars=[[latest-(35-i)*60,65000.,65025.,65010.,65015.,1.] for i in range(36)]
        with patch.object(app,"compose_coinbase_candles",return_value={
                "status":"available","candles":bars,
                "last_completed_epoch":latest+60,"candle_age_seconds":2}), \
             patch.object(app,"coinbase_candle_snapshot",return_value={}):
            result=app.coinbase_formation_candles()
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["source"],"Coinbase Exchange BTC-USD")
        self.assertEqual(result["timeframe"],"1m_completed")
        self.assertEqual(result["candles"][0],
                         [bars[0][0],65010.,65025.,65000.,65015.,1.])
        self.assertEqual(result["candles"][-1][0],latest)
        self.assertEqual(len(result["candles"]),len(bars))

    def test_unverified_source_returns_no_candles(self):
        with patch.object(app,"compose_coinbase_candles",
                          return_value={"status":"unavailable","reason":"stale",
                                        "candles":[[1,2,3,4,5,6]]}), \
             patch.object(app,"coinbase_candle_snapshot",return_value={}):
            result=app.coinbase_formation_candles()
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["candles"],[])

    def test_future_bar_rejected_without_partial_output(self):
        future=(int(time.time())//60+1)*60
        bars=[[future-(35-i)*60,65000.,65025.,65010.,65015.,1.] for i in range(36)]
        with patch.object(app,"compose_coinbase_candles",
                          return_value={"status":"available","candles":bars}), \
             patch.object(app,"coinbase_candle_snapshot",return_value={}):
            result=app.coinbase_formation_candles()
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["candles"],[])


if __name__=="__main__":
    unittest.main()
