"""One-second main chart: bounded Coinbase trade snapshots and freshness gates."""
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from live_trade_stream import TradeBuffer


class OneSecondCoinbaseChartTests(unittest.TestCase):
    def test_quote_uses_actual_coinbase_trade_without_copying_buffer(self):
        now=round(time.time(),2)
        buf=TradeBuffer()
        buf.connected(now-5)
        self.assertIsNone(buf.latest_price_snapshot(now))
        self.assertTrue(buf.append((now-2,65001,0.02,"buy","trade-1"),now))
        self.assertTrue(buf.append((now-1,65008,0.01,"sell","trade-2"),now))
        self.assertEqual(buf.latest_price_snapshot(now)["price"],65008)
        self.assertEqual(buf.latest_price_snapshot(now)["trade_epoch"],now-1)
        self.assertIsNone(buf.latest_price_snapshot(now+9))
        buf.disconnected("stream_disconnected")
        self.assertIsNone(buf.latest_price_snapshot(now))
        buf.connected(now)
        self.assertIsNone(buf.latest_price_snapshot(now))

    def test_late_old_trade_does_not_move_latest_price_backwards(self):
        now=time.time()
        buf=TradeBuffer()
        buf.connected(now-4)
        self.assertTrue(buf.append((now-1,65008,.02,"buy","trade-10"),now))
        self.assertTrue(buf.append((now-3,65000,.02,"sell","trade-9"),now))
        self.assertEqual(buf.latest_price_snapshot(now)["price"],65008)

    def test_live_price_endpoint_is_read_only_and_stale_is_explicit(self):
        with patch.object(app,"coinbase_latest_price",return_value=None) as source:
            stale=app.coinbase_formation_live_price()
        source.assert_called_once()
        self.assertEqual(stale["status"],"stale")
        self.assertIsNone(stale["price"])
        with patch.object(app,"coinbase_latest_price",return_value={
            "source":"Coinbase Exchange BTC-USD","price":65008,
            "trade_epoch":time.time(),"sampled_at":time.time()}):
            fresh=app.coinbase_formation_live_price()
        self.assertEqual(fresh["status"],"fresh")
        self.assertEqual(fresh["price"],65008)

    def test_chart_updates_each_second_but_complete_candles_stay_verified(self):
        html=Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
        self.assertIn("setInterval(()=>{refreshLivePrice();draw();},1000)",html)
        self.assertIn("setInterval(refresh,10000)",html)
        self.assertIn("/api/coinbase-formation/live-price",html)
        self.assertIn("now-tradeEpoch<=8",html)
        self.assertIn("now-sampledEpoch<=4",html)
        self.assertIn("if(document.hidden||quoteBusy",html)
        self.assertIn("let liveQuote=null,quoteBusy=false",html)
        self.assertIn("Live Coinbase: stale",html)
        self.assertIn("A live trade is a separate observed tick",html)
        self.assertIn("id=\"brtiLineCanvas\"",html)


if __name__=="__main__":
    unittest.main()
