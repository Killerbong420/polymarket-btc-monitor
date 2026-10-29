"""Real-data chart sampling and source separation regression tests."""
import unittest
import market_chart_history as chart

class ChartHistoryTest(unittest.TestCase):
 def setUp(self):
  with chart._lock:
   chart._brti.clear()
   chart._usd.clear()

 def test_real_brti_ticks_and_ranges_without_interpolation(self):
  now=12000.0
  chart.record_brti([[11990,81000],[11991,81001],[11994,81004],[11994,81006],
                     [11995,81005],[11997,-5],[11998,float("nan")]],"Polymarket US BTC reference / BRTI",now=now)
  result=chart.snapshot("30s",round_start=11700,round_end=12600,market="btc-updown-15m-a",now=now)
  self.assertEqual([x[0] for x in result["brti_points"]],[11990,11991,11994,11995])
  self.assertEqual(result["brti_points"][2][1],81006)
  self.assertEqual(result["status"],"fresh")
  self.assertFalse(any(x[0]==11992 for x in result["brti_points"]))
  assert len(result["venue_series"])==0

 def test_only_verified_source_and_recent_samples(self):
  chart.record_brti([[11999,85000]],"Coinbase",now=12000)
  self.assertEqual(chart.snapshot("round",round_start=11100,now=12000)["status"],"collecting")
  chart.record_brti([[8300,80000],[11990,80300],[11991,80301]],"Polymarket US BTC reference / BRTI",now=12000)
  snapshot=chart.snapshot("60m",round_start=11100,now=12000)
  self.assertEqual(len(snapshot["brti_points"]),2)
  chart.record_brti([[12010,80322]],"Polymarket US BTC reference / BRTI",now=12035)
  stale=chart.snapshot("round",round_start=11100,now=12035)
  self.assertEqual(stale["status"],"stale")

 def test_exchange_prices_are_usd_only_and_outliers_excluded(self):
  now=12000.0
  good={"status":"available","exchanges":[
    {"source":"Coinbase","quote":"USD","price":81000,"status":"responding","fetched_epoch":11996.0},
    {"source":"Kraken","quote":"USD","price":81003,"status":"responding","fetched_epoch":11996.0},
    {"source":"Binance Global","quote":"USDT","price":81080,"status":"responding","fetched_epoch":11996.0},
    {"source":"Bitstamp","quote":"USD","price":90000,"status":"outlier","fetched_epoch":11996.0},
    {"source":"Gemini","quote":"USD","price":80990,"status":"stale","fetched_epoch":11940.0}]}
  chart.record_exchange(good,now)
  data=chart.snapshot("30s",now=now)
  self.assertEqual({x["source"] for x in data["venue_series"]},{"Coinbase","Kraken"})
  self.assertEqual(data["venue_series"][0]["status"],"fresh")
  later=chart.snapshot("5m",now=12060)
  self.assertEqual({x["status"] for x in later["venue_series"]},{"stale"})
  self.assertEqual(chart.snapshot("60m",now=16000)["venue_series"],[])

if __name__=="__main__":unittest.main()
