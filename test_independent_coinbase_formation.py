"""Coinbase Formation Watch remains independent from the Kraken main-spot fallback."""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
from test_early_pattern_context import make_bars


def fresh_bars():
    bars=make_bars(1)
    latest_open=(int(time.time())//60-1)*60
    offset=latest_open-bars[-1][0]
    for bar in bars:
        bar[0]+=offset
    return bars


def kraken_ohlc(bars):
    records=[[b[0],b[3],b[2],b[1],b[4],b[4],max(b[5],10),10] for b in bars]
    records.append([bars[-1][0]+60,bars[-1][4],bars[-1][2],
                    bars[-1][1],bars[-1][4],bars[-1][4],1,1])
    return {"error":[],"result":{"XXBTZUSD":records,"last":bars[-1][0]}}


class FakeClient:
    def __init__(self,bars,coinbase_failure=False):
        self.bars=bars
        self.coinbase_failure=coinbase_failure
        self.calls=[]

    async def get(self,url,**kwargs):
        self.calls.append(url)
        if "coinbase" in url:
            if self.coinbase_failure:
                raise RuntimeError("temporary Coinbase API timeout")
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:self.bars)
        if "kraken" in url:
            return SimpleNamespace(raise_for_status=lambda:None,
                                   json=lambda:kraken_ohlc(self.bars))
        raise AssertionError("unexpected request")


class IndependentCoinbaseFormationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved=dict(app._coinbase_formation_cache)
        app._coinbase_formation_cache["candles"]=None

    async def asyncTearDown(self):
        app._coinbase_formation_cache.clear()
        app._coinbase_formation_cache.update(self.saved)

    async def kraken_fallback(self,bars,coinbase_failure=False):
        client=FakeClient(bars,coinbase_failure)
        with patch.object(app,"chart_indicators",return_value={}), \
             patch.object(app,"classify_chart",return_value={"pattern":"RANGE"}), \
             patch.object(app,"graph_evidence",return_value={}), \
             patch.object(app,"analyze_price_action",return_value={}), \
             patch.object(app,"study_strategies",return_value={}), \
             patch.object(app,"measure_market_shock",return_value={}):
            result=await app.spot_data(client)
        self.assertEqual(len(client.calls),2)  # no additional Coinbase requests
        return result

    async def test_recent_real_coinbase_candles_survive_temporary_spot_fallback(self):
        bars=fresh_bars()
        app._coinbase_formation_cache["candles"]=bars[-50:]
        result=await self.kraken_fallback(bars,coinbase_failure=True)
        self.assertEqual(result["source"],"Kraken BTC/USD")
        self.assertEqual(result["early_patterns"]["source"],"Coinbase Exchange BTC-USD")
        self.assertIn(result["early_patterns"]["status"],("watch","monitoring"))
        self.assertEqual(result["pattern_candle"],bars[-1])
        self.assertEqual(result["formation_candles"],bars[-5:])
        self.assertEqual(result["coinbase_feed_health"]["status"],"rest_verified")
        self.assertIn("temporary Coinbase",result["coinbase_feed_health"]["last_error"])

    async def test_coinbase_volume_filter_does_not_suppress_valid_formation(self):
        bars=fresh_bars()
        for b in bars:b[5]=.001
        result=await self.kraken_fallback(bars)
        self.assertEqual(result["source"],"Kraken BTC/USD")
        self.assertIn(result["early_patterns"]["status"],("watch","monitoring"))
        self.assertEqual(result["early_patterns"]["source"],"Coinbase Exchange BTC-USD")
        self.assertEqual(result["coinbase_feed_health"]["status"],"rest_verified")

    async def test_stale_coinbase_never_masquerades_as_live(self):
        bars=fresh_bars()
        for b in bars:b[0]-=600
        app._coinbase_formation_cache["candles"]=bars
        result=await self.kraken_fallback(fresh_bars(),coinbase_failure=True)
        self.assertEqual(result["source"],"Kraken BTC/USD")
        self.assertEqual(result["early_patterns"]["status"],"unavailable")
        self.assertEqual(result["early_patterns"]["reason"],"coinbase_feed_unavailable")
        self.assertIsNone(result["pattern_candle"])
        self.assertEqual(result["formation_candles"],[])

    def test_ui_uses_source_specific_age_and_does_not_invent_kraken_formations(self):
        from pathlib import Path
        html=Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
        block=html.split("function renderPatterns()",1)[1].split("</script>",1)[0]
        self.assertIn("const candleAge=Number(p.candle_age_seconds)",block)
        self.assertIn("p.source==='Coinbase Exchange BTC-USD'",block)
        self.assertNotIn("d.source==='Coinbase Exchange BTC-USD'",block)
        self.assertIn("x.source===p.source",block)
        self.assertNotIn("fetch(",block)


if __name__=="__main__":
    unittest.main()
