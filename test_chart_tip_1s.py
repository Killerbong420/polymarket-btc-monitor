"""Verify one-second BRTI chart sampler never invents values or bypasses settlement."""
import asyncio
import time
import unittest
from datetime import datetime,timezone

from market_chart_history import (
    _brti,_lock,OFFICIAL_SOURCE,eligible_tick_market,
    latest_tip,record_brti,sample_official_ticks
)

class ChartTipTest(unittest.TestCase):
 def setUp(self):
  with _lock:_brti.clear()

 def _state(self,now):
  start=int(now//900)*900
  return {"status":"running","market":"cpc-btc-updown-15m-test",
    "updated":datetime.fromtimestamp(now,timezone.utc).isoformat(),
    "reference_price":{"status":"fresh","source":OFFICIAL_SOURCE,
      "window_start":start,"window_end":start+900,"price_epoch":now-2}}

 def test_market_identity_and_heartbeat_guard(self):
  now=int(time.time()//900)*900+300
  data=self._state(now)
  self.assertTrue(eligible_tick_market(data,now))
  self.assertFalse(eligible_tick_market({**data,"status":"starting"},now))
  self.assertFalse(eligible_tick_market({**data,"market":"BTCUSD"},now))
  self.assertFalse(eligible_tick_market({**data,"reference_price":{**data["reference_price"],"source":"Coinbase"}},now))
  self.assertFalse(eligible_tick_market({**data,"reference_price":{**data["reference_price"],"price_epoch":now-100}},now))
  self.assertFalse(eligible_tick_market({**data,"updated":"broken"},now))
  self.assertFalse(eligible_tick_market(data,data["reference_price"]["window_end"]))

 def test_tip_shows_observed_seconds_only(self):
  now=12000
  record_brti([[11995,81000],[11998,81020],[11997,0]],OFFICIAL_SOURCE,now=now)
  data=latest_tip(now)
  self.assertEqual(data["status"],"fresh")
  self.assertEqual(data["points"],[[11995,81000],[11998,81020]])
  self.assertNotIn([11996,81000],data["points"])
  self.assertEqual(latest_tip(12050)["status"],"stale")

 def test_one_tick_api_poll_filters_bad_source_and_invalid_tick_count(self):
  now=time.time()
  market=self._state(now)
  # This may be near the actual 15-minute boundary; provide a correct start
  # and skip the HTTP-stage validation if the clock is in the 2-second rollover.
  if (now%900)>897:self.skipTest("boundary rollover")
  valid=int(now)-3
  class Response:
   def __init__(self,body):self.body=body
   def raise_for_status(self):return None
   def json(self):return self.body
  class Client:
   def __init__(self,body):self.body=body;self.calls=0
   async def get(self,url,params=None,timeout=None):
    self.calls+=1
    assert url.startswith("https://gateway.polymarket.us/")
    assert params["bucket"]=="ASSET_PRICE_BUCKET_1S"
    return Response(self.body)
  body={"series":[{"asset":{"symbol":"btc","assetClass":"ASSET_CLASS_CRYPTO"},
   "currency":"USD","candles":[
    {"timestamp":valid,"close":81000.02,"tickCount":1},
    {"timestamp":valid+1,"close":99999,"tickCount":0},
    {"timestamp":valid+2,"close":-5,"tickCount":1}]}]}
  client=Client(body)
  n=asyncio.run(sample_official_ticks(client,market,now=now))
  self.assertEqual(n,1)
  self.assertEqual(client.calls,1)
  self.assertEqual(latest_tip()["points"][-1],[valid,81000.02])
  self.assertEqual(asyncio.run(sample_official_ticks(client,{**market,"status":"stale"},now=now)),0)
  self.assertEqual(client.calls,1)

if __name__=="__main__":unittest.main()
