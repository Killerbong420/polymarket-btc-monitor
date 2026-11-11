"""No synthesized price bars: Coinbase live candles must be complete and causal."""
import unittest
from coinbase_live_candles import CoinbaseMinuteBuilder, compose_coinbase_candles


BASE=2400000


def beat(builder,ident,when):
    builder.heartbeat({"type":"heartbeat","product_id":"BTC-USD",
                       "last_trade_id":ident,"time_epoch":when},now=when)


def trade(builder,ident,when,price,amount=1):
    return builder.append((when,price,amount,"buy",ident),now=when)


def feed_minute(builder,events=None,until=70,skip_heartbeats=(),heartbeat_overrides=None):
    # Simulate the actual ordered stream: heartbeat once a second, with
    # qualifying match messages interspersed before that second's heartbeat.
    events=events or {}
    heartbeat_overrides=heartbeat_overrides or {}
    for sec in range(-2,until+1):
        now=BASE+sec
        if sec in events:
            for ident,price in events[sec]:
                trade(builder,ident,now,price)
        if sec not in skip_heartbeats:
            beat(builder,heartbeat_overrides.get(sec,builder.last_trade_id),now)


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
        feed_minute(b,{1:[(101,100)],10:[(102,110)],55:[(103,105)]},until=61)
        self.assertEqual(b.snapshot(BASE+61)["status"],"warming")
        for sec in range(62,71):beat(b,b.last_trade_id,BASE+sec)
        ready=b.snapshot(BASE+70)
        self.assertEqual(ready["status"],"available")
        self.assertEqual(ready["transport"],"websocket_matches_heartbeat")
        bar=ready["candles"][-1]
        self.assertEqual(bar[:5],[BASE,100,110,100,105])
        self.assertEqual(bar[5],3)
        self.assertEqual(ready["last_completed_epoch"],BASE+60)

    def test_id_gap_rejects_candle_without_interpolating(self):
        b=self.new_builder()
        feed_minute(b,{3:[(101,100)],10:[(103,110)]})
        result=b.snapshot(BASE+70)
        self.assertEqual(result["status"],"warming")
        self.assertEqual(result["candles"],[])
        self.assertIn(BASE,b.bad_minutes)

    def test_heartbeat_detects_lost_match(self):
        b=self.new_builder()
        feed_minute(b,{2:[(101,100)]},heartbeat_overrides={59:103})  # missed 102+103
        self.assertEqual(b.snapshot(BASE+70)["candles"],[])

    def test_heartbeat_gap_rejects_minute(self):
        b=self.new_builder()
        feed_minute(b,{1:[(101,100)]},skip_heartbeats=set(range(2,18)))
        self.assertEqual(b.snapshot(BASE+70)["candles"],[])

    def test_disconnect_clears_all_unreliable_stream_bars(self):
        b=self.new_builder()
        feed_minute(b,{1:[(101,100)]})
        self.assertEqual(len(b.snapshot(BASE+70)["candles"]),1)
        b.disconnected("connection_closed")
        self.assertEqual(b.snapshot(BASE+71)["status"],"unavailable")
        self.assertEqual(b.snapshot(BASE+71)["candles"],[])
        b.connected(BASE+73)  # cannot complete partial reconnect minute
        beat(b,105,BASE+74)
        for sec in range(75,131):
            now=BASE+sec
            if sec==75:trade(b,106,now,107)
            beat(b,b.last_trade_id,now)
        self.assertEqual(b.snapshot(BASE+130)["candles"],[])

    def test_only_completed_minute_not_current_partial(self):
        b=self.new_builder()
        feed_minute(b,{1:[(101,100)],62:[(102,115)]})
        result=b.snapshot(BASE+70)
        self.assertEqual([x[0] for x in result["candles"]],[BASE])

    def test_stale_heartbeat_cannot_sign_complete_bar(self):
        b=self.new_builder()
        feed_minute(b,{4:[(101,100)]},until=59)
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

    def test_matching_rest_and_websocket_prefers_heartbeat_checked_live_bar(self):
        rest=rest_history(last_start=BASE,count=36)
        same_bar=rest[-1].copy()
        stream={"status":"available","candles":[same_bar]}
        result=compose_coinbase_candles(rest,stream,now=BASE+70)
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["transport"],"websocket_verified")
        self.assertEqual(result["websocket_minutes"],1)
        self.assertEqual(result["rest_reconciliation_conflicts"],0)

    def test_rest_overrides_conflicting_websocket_for_same_minute(self):
        rest=rest_history(last_start=BASE,count=36)
        websocket={"status":"available","candles":[[BASE,98,125,100,119,9]]}
        result=compose_coinbase_candles(rest,websocket,now=BASE+70)
        self.assertEqual(result["status"],"available")
        self.assertEqual(result["transport"],"rest_verified")
        self.assertEqual(result["candles"][-1][4],100.5)
        self.assertEqual(result["rest_reconciliation_conflicts"],1)

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
