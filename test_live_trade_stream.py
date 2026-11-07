"""Offline regression tests for bounded live exchange execution research."""
import unittest
from unittest.mock import patch

import live_trade_stream as live
import market_intelligence as intel


class TradeStreamTests(unittest.TestCase):
    def test_coinbase_maker_side_inverted_only_for_new_match(self):
        message={"type":"match","product_id":"BTC-USD","time":"2026-11-07T20:00:00Z",
                 "price":"100","size":"2","side":"sell","trade_id":77}
        result=live.parse_trade_events("Coinbase",message)
        self.assertEqual(result[0][3],"buy")
        self.assertEqual(result[0][4],77)
        self.assertEqual(live.parse_trade_events("Coinbase",{**message,"type":"last_match"}),[])
        self.assertEqual(live.parse_trade_events("Coinbase",{**message,"product_id":"ETH-USD"}),[])

    def test_kraken_updates_only_and_taker_side_preserved(self):
        message={"channel":"trade","type":"update","data":[
            {"symbol":"BTC/USD","timestamp":"2026-11-07T20:00:00Z",
             "price":101,"qty":1,"side":"sell","trade_id":12},
            {"symbol":"ETH/USD","timestamp":"2026-11-07T20:00:00Z",
             "price":101,"qty":1,"side":"buy","trade_id":13}]}
        self.assertEqual(len(live.parse_trade_events("Kraken",message)),1)
        self.assertEqual(live.parse_trade_events("Kraken",message)[0][3],"sell")
        self.assertEqual(live.parse_trade_events("Kraken",{**message,"type":"snapshot"}),[])
        self.assertEqual(live.parse_trade_events("Kraken",{"type":"update","channel":"trade","data":None}),[])

    def test_dedup_bounded_stale_and_reconnect(self):
        buf=live.TradeBuffer()
        now=1000.0
        buf.connected(now-50)
        self.assertTrue(buf.append((now-31,100,1,"buy","a"),now))
        self.assertFalse(buf.append((now-31,100,1,"buy","a"),now))
        self.assertTrue(buf.append((now-1,100,1,"sell","b"),now))
        self.assertFalse(buf.append((now-1,0,1,"buy","c"),now))
        self.assertFalse(buf.append((now-1,100,1,"unknown","c"),now))
        self.assertEqual(len(buf.snapshot(now)["rows"]),2)
        self.assertIsNone(buf.snapshot(now+12))
        buf.disconnected("connection_closed")
        self.assertIsNone(buf.snapshot(now))
        self.assertEqual(len(buf.rows),0)
        buf.connected(now+13)
        self.assertIsNone(buf.snapshot(now+13))

    def test_overflow_marks_windows_partial(self):
        now=20000.0
        buffer=live.TradeBuffer()
        buffer.connected(now-100)
        for i in range(live.MAX_TRADES+1):
            buffer.append((now-31+i*30/(live.MAX_TRADES+1),100,1,"buy",i),now)
        self.assertLessEqual(len(buffer.rows),live.MAX_TRADES)
        self.assertIsNotNone(buffer.overflow_epoch)
        sample=buffer.snapshot(now)
        with patch.object(intel,"stream_snapshot",return_value=sample):
            result=intel.streaming_flow("Coinbase",now)
        self.assertEqual(result["status"],"fresh")
        self.assertEqual(result["short_windows"]["30s"]["status"],"partial")

    def test_streamed_30_second_coverage_needs_warmup(self):
        now=1000
        sample={"rows":[(now-29,100,2,"buy","1"),(now-15,100,1,"sell","2"),
                        (now-1,100,1,"buy","3")],
                "connected_since_epoch":now-60,"overflow_epoch":None,
                "latest_trade_epoch":now-1}
        with patch.object(intel,"stream_snapshot",return_value=sample):
            live_summary=intel.streaming_flow("Coinbase",now)
        self.assertEqual(live_summary["short_windows"]["30s"]["status"],"observed")
        self.assertAlmostEqual(live_summary["short_windows"]["30s"]["imbalance"],0.5)
        with patch.object(intel,"stream_snapshot",return_value={**sample,"connected_since_epoch":now-5}):
            partial=intel.streaming_flow("Coinbase",now)
        self.assertEqual(partial["short_windows"]["30s"]["status"],"partial")

    def test_rest_fallback_and_stream_overlay_do_not_double_count(self):
        now=1000
        rest={"source":"Coinbase","status":"fresh","buy_usd":500,"sell_usd":0,
              "latest_trade_epoch":999,"transport":"rest_sample"}
        other={"source":"Kraken","status":"unavailable"}
        with patch.object(intel,"stream_snapshot",return_value=None):
            with patch.dict(intel._state,{"trade_flow":{"status":"available",
                    "exchanges":[rest,other],"fetched_epoch":999}}):
                result=intel.public_trade_flow(now)
                self.assertEqual(result["transport"],"rest_fallback")
                self.assertEqual(result["exchanges"][0]["buy_usd"],500)
        sample={"rows":[(999,100,1,"sell","a")],"connected_since_epoch":960,
                "overflow_epoch":None,"latest_trade_epoch":999}
        def pick(source, epoch):
            return sample if source=="Coinbase" else None
        with patch.object(intel,"stream_snapshot",side_effect=pick):
            with patch.dict(intel._state,{"trade_flow":{"status":"available",
                    "exchanges":[rest,other],"fetched_epoch":999}}):
                result=intel.public_trade_flow(now)
                self.assertEqual(result["exchanges"][0]["transport"],"websocket")
                self.assertEqual(result["exchanges"][0]["buy_usd"],0)
                self.assertEqual(result["exchanges"][0]["sell_usd"],100)

    def test_excludes_trades_outside_retention_and_future(self):
        b=live.TradeBuffer();b.connected(1000)
        self.assertFalse(b.append((800,100,1,"buy","old"),1000))
        self.assertFalse(b.append((1003,100,1,"buy","future"),1000))
        self.assertTrue(b.append((999,100,1,"buy","recent"),1000))
        self.assertEqual(len(b.rows),1)


if __name__ == "__main__":
    unittest.main()
