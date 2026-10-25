import asyncio, copy, unittest
from unittest.mock import AsyncMock, patch
import httpx
import app

class CandleTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.saved=copy.deepcopy(app._reference_candle_cache)
  app._reference_candle_cache.update(at=0,payload=None)
  self.lock=patch.object(app,'_reference_candle_lock',asyncio.Lock());self.lock.start()
  self.now=1800000
 def tearDown(self):
  app._reference_candle_cache.update(self.saved);self.lock.stop()
 def candle(self,**kw):
  return dict(timestamp=self.now-60,open='100',high='110',low='90',close='105',tickCount=60,**kw)
 async def request(self,rows=None,error=None):
  client=AsyncMock()
  response=httpx.Response(200,request=httpx.Request('GET','https://example.test'),json={'series':[{'asset':{'symbol':'btc','assetClass':'ASSET_CLASS_CRYPTO'},'currency':'USD','candles':rows or []}]})
  client.get=AsyncMock(return_value=response,side_effect=error)
  factory=AsyncMock();factory.__aenter__.return_value=client
  with patch('app.httpx.AsyncClient',return_value=factory),patch('app.time.time',return_value=self.now):
   payload=await app.candle_proxy()
  return payload,client
 async def test_bad_rows_do_not_discard_good_candles(self):
  valid=self.candle()
  payload,_=await self.request([None,{**valid,'tickCount':'bad'},{**valid,'timestamp':self.now-120,'tickCount':0},{**valid,'close':'NaN'},valid])
  self.assertEqual(payload['status'],'fresh');self.assertEqual(payload['candles'],[[self.now-60,90,110,100,105,0]])
  self.assertFalse(payload['volume_available'])
 async def test_stale_and_empty_feeds(self):
  payload,_=await self.request([{**self.candle(),'timestamp':self.now-180}])
  self.assertEqual(payload['status'],'stale')
  app._reference_candle_cache.update(at=0,payload=None)
  payload,_=await self.request([])
  self.assertEqual(payload['status'],'unavailable');self.assertEqual(payload['error_code'],'INVALID_FEED_DATA')
 async def test_http_and_timeout_diagnostics(self):
  req=httpx.Request('GET','https://example.test')
  error=httpx.HTTPStatusError('sensitive detail',request=req,response=httpx.Response(429,request=req))
  payload,_=await self.request(error=error)
  self.assertEqual(payload['error_code'],'HTTP_429');self.assertNotIn('sensitive',str(payload))
  app._reference_candle_cache.update(at=0,payload=None)
  payload,_=await self.request(error=httpx.ReadTimeout('private detail'))
  self.assertEqual(payload['error_code'],'TIMEOUT')
 async def test_cache_avoids_second_request(self):
  payload,_=await self.request([self.candle()])
  cached,client=await self.request(error=RuntimeError('should not request'))
  self.assertEqual(cached,payload);client.get.assert_not_called()
 def test_zero_second_reference_age_is_fresh(self):
  with patch.dict(app.state,{'market':'m','brti_chart':{'market':'m','points':[[self.now,100]],'sample_age_seconds':0,'observed_epoch':self.now}},clear=True),patch('app.time.time',return_value=self.now):
   self.assertEqual(app.brti_chart()['status'],'fresh')

if __name__=='__main__':unittest.main()
