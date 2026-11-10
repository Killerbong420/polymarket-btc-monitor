"""Read-only BTC/USD large-execution watch: measured trades, not whale identity."""
import unittest
from pathlib import Path
from unittest.mock import patch

from large_execution_watch import examine_stream, combine_watches
import market_intelligence as intel

NOW=20000.0


def sample(side=None, connected=None, overflow=None, recent=True, big_usd=750000):
    # Sufficient continuous 115-second historical baseline separated from 30s event window.
    rows=[(NOW-t,75000,.05,"buy" if t%2 else "sell",f"b{t}") for t in range(36,151)]
    if recent:
        rows.extend((NOW-t,75000,.04,"sell" if t%2 else "buy",f"r{t}")
                    for t in range(1,31))
    if side:
        if side=="mixed":
            rows.extend([(NOW-4,75000,big_usd/75000,"buy","huge-buy"),
                         (NOW-3,75000,big_usd/75000,"sell","huge-sell")])
        else:
            rows.append((NOW-4,75000,big_usd/75000,side,"huge"))
    return {"rows":rows, "connected_since_epoch":connected if connected is not None else NOW-180,
            "latest_trade_epoch":NOW-1 if recent else NOW-38,
            "overflow_epoch":overflow}


class WhaleSizedTradeWatchTests(unittest.TestCase):
    def test_large_aggressive_buy_observed_not_identified(self):
        watch=examine_stream(sample("buy"),"Coinbase",NOW)
        self.assertEqual(watch["status"],"watch")
        self.assertEqual(watch["side"],"BUY_SIDE")
        self.assertEqual(watch["large_execution_count"],1)
        self.assertEqual(watch["largest_execution_usd"],750000)
        self.assertGreaterEqual(watch["threshold_usd"],250000)
        self.assertTrue(watch["observation_only"])
        self.assertNotIn("probability",watch)
        self.assertNotIn("whale_address",watch)

    def test_sell_and_mixed_are_not_bullish_without_evidence(self):
        self.assertEqual(examine_stream(sample("sell"),"Kraken",NOW)["side"],"SELL_SIDE")
        self.assertEqual(examine_stream(sample("mixed"),"Kraken",NOW)["side"],"MIXED")

    def test_no_large_trade_means_monitoring_not_buy_pressure(self):
        watch=examine_stream(sample(),"Coinbase",NOW)
        self.assertEqual(watch["status"],"monitoring")
        self.assertEqual(watch["large_execution_count"],0)
        self.assertEqual(watch["side"],"NONE")

    def test_stale_reconnect_overflow_and_gaps_fail_closed(self):
        cases=[
            (sample("buy",connected=NOW-40),"collecting"),
            (sample("buy",overflow=NOW-10),"partial"),
            (sample("buy",recent=False),"unavailable"),
            (sample("buy",connected=NOW+10),"unavailable"),
        ]
        for example,expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(examine_stream(example,"Coinbase",NOW)["status"],expected)
        gap=sample("buy")
        gap["rows"]=[x for x in gap["rows"] if x[0]>NOW-20 or x[0]<NOW-150]
        self.assertNotEqual(examine_stream(gap,"Coinbase",NOW)["status"],"watch")
        self.assertEqual(examine_stream({}, "unknown", NOW)["status"],"unavailable")

    def test_cross_venue_aggregate_and_book_context_do_not_claim_whales(self):
        buying=examine_stream(sample("buy"),"Coinbase",NOW)
        quiet=examine_stream(sample(),"Kraken",NOW)
        flow={"status":"available","exchanges":[
            {"source":"Coinbase","status":"fresh","transport":"websocket","large_trade_watch":buying},
            {"source":"Kraken","status":"fresh","transport":"websocket","large_trade_watch":quiet}]}
        book={"status":"fresh","source":"Kraken BTC/USD","fetched_epoch":NOW-2,
              "imbalance":-.6}
        out=combine_watches(flow,book,NOW)
        self.assertEqual(out["status"],"watch")
        self.assertEqual(out["side"],"BUY_SIDE")
        self.assertEqual(out["large_execution_count"],1)
        self.assertEqual(out["resting_liquidity_imbalance_pct"],-60.0)
        self.assertTrue(out["not_whale_identity"])
        self.assertFalse(out["changes_primary_prediction"])
        self.assertFalse(out["probability_calibrated"])
        self.assertNotIn("direction_prediction",out)
        self.assertNotIn("buy_signal",out)

    def test_rest_only_and_book_only_do_not_generate_watch(self):
        flow={"status":"available","exchanges":[
          {"source":"Coinbase","status":"fresh","transport":"rest_sample",
           "large_trade_watch":examine_stream(sample("buy"),"Coinbase",NOW)}]}
        out=combine_watches(flow,{"status":"fresh","source":"Kraken BTC/USD",
                "fetched_epoch":NOW,"imbalance":.9},NOW)
        self.assertEqual(out["status"],"collecting")
        self.assertNotIn("side",out)

    def test_stream_integration_and_ui_no_extra_requests(self):
        # The existing stream parser and REST fallback remain the sole sources.
        with patch.object(intel,"stream_snapshot",return_value=sample("sell")):
            watch=intel.streaming_flow("Kraken",NOW)["large_trade_watch"]
        self.assertEqual(watch["status"],"watch")
        self.assertEqual(watch["side"],"SELL_SIDE")
        root=Path(__file__).resolve().parent
        html=(root/"terminal.html").read_text()
        backend=(root/"market_intelligence.py").read_text()
        self.assertIn("from large_execution_watch import examine_stream, combine_watches",backend)
        self.assertIn("d['large_trade_activity']=combine_watches",backend)
        self.assertIn("function whaleContext(s,now)",html)
        self.assertIn("LARGE-TRADE WATCH",html)
        self.assertIn("const doji=dojiContext(s,now),whale=whaleContext(s,now)",html)
        self.assertIn("rule-based fit score, NOT outcome odds'+doji+whale",html)
        self.assertNotIn('id="whaleWatch"',html)
        part=html.split("function whaleContext(s,now)",1)[1].split("</script>",1)[0]
        self.assertNotIn("fetch(",part)
        self.assertNotIn("meterUpPct",part)
        self.assertNotIn("meterDownPct",part)


if __name__=="__main__":
    unittest.main()
