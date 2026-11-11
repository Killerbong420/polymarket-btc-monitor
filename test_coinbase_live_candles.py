"""No synthesized price bars: Coinbase live candles must be complete and causal."""
import unittest
from coinbase_live_candles import CoinbaseMinuteBuilder, compose_coinbase_candles


BASE=2400000


def beat(builder,ident,when):
    builder.heartbeat({"type":"heartbeat","product_id":"BTC-USD",
                       "last_trade_id":ident,"time_epoch":when},now=when)


def trade(builder,ident,when,price,amount=1):
    return builder.append((when,price,amount,"buy",ident),now=when)


def rest_history(last_start=BASE-60,count=35):
    return [[last_start-(count-1-i)*60,100,101,100.1,100.5,3]
            for i in range(count)]


class CoinbaseMinuteBuilderTests(unittest.TestCase):
    def new_builder(self):
        b=CoinbaseMinuteBuilder()
        b.connected(BASE-5)
        beat(b,100,BASE-3)
        return b

    def test_candle_waits_for_true_close_and_heartbeat(self):
        b=self.new_builder()
        self.assertTrue(trade(b,101,BASE+1,100,2))
        self.assertTrue(trade(b,102,BASE+10,110,1))
        self.assertTrue(trade(b,103,BASE+55,105,3))
        beat(b,103,BASE+58)
        self.assertEqual(b.snapshot(BASE+61)["status"],"warming")
        beat(b,103,BASE+69)
        ready=b.snapshot(BASE+70)
        self.assertEqual(ready["status"],"available")
        self.assertEqual(ready["transport"],"websocket_matches_heartbeat")
        bar=ready["candles"][-1]
        self.assertEqual(bar[:5],[BASE,100,110,100,105])
        self.assertEqual(bar[5],6)
        self.assertEqual(ready["last_completed_epoch"],BASE+60)

    def test_id_gap_rejects_candle_without_interpolating(self):
        b=self.new_builder()
        self.assertTrue(trade(b,101,BASE+3,100))
        self.assertTrue(trade(b,103,BASE+10,110))
        beat(b,103,BASE+69)
        result=b.snapshot(BASE+70)
        self.assertEqual(result["status"],"warming")
        self.assertEqual(result["candles"],[])
        self.assertIn(BASE,b.bad_minutes)

    def test_heartbeat_detects_lost_match(self):
        b=self.new_builder()
        self.assertTrue(trade(b,101,BASE+2,100))
        beat(b,103,BASE+59)  # last trade 103 reported but 102+103 unseen
        beat(b,103,BASE+69)
        self.assertEqual(b.snapshot(BASE+70)["candles"],[])

    def test_heartbeat_gap_rejects_minute(self):
        b=self.new_builder()
        self.assertTrue(trade(b,101,BASE+1,100))
        beat(b,101,BASE+18)  # 21-second heartbeat outage
        beat(b,101,BASE+69)
        self.assertEqual(b.snapshot(BASE+70)["candles"],[])

    def test_disconnect_clears_all_unreliable_stream_bars(self):
        b=self.new_builder()
        trade(b,101,BASE+1,100)
        beat(b,101,BASE+69)
        self.assertEqual(len(b.snapshot(BASE+70)["candles"]),1)
        b.disconnected("connection_closed")
        self.assertEqual(b.snapshot(BASE+71)["status"],"unavailable")
        self.assertEqual(b.snapshot(BASE+71)["candles"],[])
        b.connected(BASE+73)  # cannot complete partial reconnect minute
        beat(b,105,BASE+74)
        trade(b,106,BASE+75,107)
        beat(b,106,BASE+129)
        self.assertEqual(b.snapshot(BASE+130)["candles"],[])

    def test_only_completed_minute_not_current_partial(self):
        b=self.new_builder()
        trade(b,101,BASE+1,100)
        beat(b,101,BASE+59)
        trade(b,102,BASE+62,115)
        beat(b,102,BASE+69)
        result=b.snapshot(BASE+70)
        self.assertEqual([x[0] for x in result["candles"]],[BASE])

    def test_stale_heartbeat_cannot_sign_complete_bar(self):
        b=self.new_builder()
        trade(b,101,BASE+4,100)
        beat(b,101,BASE+59)
        self.assertEqual(b.snapshot(BASE+70)["reason"],"coinbase_heartbeat_stale")


class VerifiedRestReconciliationTests(unittest.TestCase):
    def test_websocket_completed_candle_extends_rest_contiguously(self):
        rest=rest_history()
        stream={"status":"available","candles":[[BASE,98,111,100,109,5]]}
        result=compose_coinbase_candles(rest,stream,now=BASE+70)
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["transport"],"websocket_verified")
        self.assertEqual(result["verified_minutes"],36)
        self.assertEqual(result["websocket_minutes"],1)
        self.assertEqual(result["candles"][-1],[BASE,98,111,100,109,5])

    def test_rest_recovers_missing_websocket_minute(self):
        rest=rest_history(last_start=BASE,count=36)
        stream={"status":"unavailable","candles":[]}
        result=compose_coinbase_candles(rest,stream,now=BASE+70)
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["transport"],"rest_verified")
        self.assertEqual(result["websocket_minutes"],0)

    def test_no_fabricated_gap_filling(self):
        rest=rest_history(last_start=BASE-120,count=35)
        stream={"status":"available","candles":[[BASE,98,111,100,109,5]]}
        result=compose_coinbase_candles(rest,stream,now=BASE+70)
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["verified_minutes"],1)
        self.assertEqual(result["reason"],"need_35_contiguous_coinbase_candles")

    def test_rest_overrides_conflicting_websocket_for_same_minute(self):
        rest=rest_history(last_start=BASE,count=36)
        websocket={"status":"available","candles":[[BASE,98,125,100,119,9]]}
        result=compose_coinbase_candles(rest,websocket,now=BASE+70)
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["transport"],"rest_verified")
        self.assertEqual(result["candles"][-1][4],100.5)

    def test_stale_rest_and_stale_websocket_never_display_as_live(self):
        rest=rest_history(last_start=BASE-120)
        stream={"status":"available","candles":[[BASE-60,98,111,100,109,5]]}
        result=compose_coinbase_candles(rest,stream,now=BASE+90)
        self.assertEqual(result["status"],"unavailable")
        self.assertEqual(result["reason"],"coinbase_completed_candle_stale")

    def test_no_current_or_invalid_candles_accepted(self):
        rest=rest_history()
        stream={"status":"available","candles":[[BASE,80,120,90,110,0],
                                                 [BASE+60,80,120,90,110,1]]}
        result=compose_coinbase_candles(rest,stream,now=BASE+50)
        self.assertEqual(result["transport"],"rest_verified")
        self.assertEqual(result["candles"][-1][0],BASE-60)


if __name__=="__main__":
    unittest.main()
