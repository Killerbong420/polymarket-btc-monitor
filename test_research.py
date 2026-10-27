import asyncio, copy, json, os, tempfile, time, unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import AsyncMock,patch
import app
import research as r
import market_intelligence as intel
from fastapi.testclient import TestClient

class Reply:
 def __init__(self,data,status=200):self.data=data;self.status_code=status
 def json(self):return self.data
 def raise_for_status(self):
  if self.status_code!=200:raise ValueError('HTTP error')

class PureTests(unittest.TestCase):
 def test_no_checkpoint_backfill(self):
  self.assertEqual(r.checkpoint_plan(179,set()),{'due':['LATE'],'missed':['EARLY','HALFWAY']})
  self.assertEqual(r.checkpoint_plan(450,{'HALFWAY'})['due'],[])
  self.assertEqual(r.checkpoint_plan(659,set())['due'],[])
 def test_fees_and_payoff(self):
  c=r.entry_cost(.5,100,0)
  self.assertEqual(c['fee_usd'],1.74);self.assertEqual(c['total_cost_usd'],51.74)
  self.assertAlmostEqual(c['net_if_win_usd'],48.26)
  self.assertEqual(r.entry_cost(.99,100,.01)['status'],'unavailable')
  self.assertEqual(r.entry_cost(float('nan'))['status'],'unavailable')
  self.assertEqual(r.entry_cost(.1,1000,0)['fee_usd'],6.26)
 def test_target_coverage_and_no_future(self):
  slug='cpc-btc-updown-15m-2026-10-25-1400z';start=r.market_start(slug)
  ref={'status':'fresh','source':'reference','points':[(start,101),(start+1,99),(start+3,101),(start+30,99)]}
  b=r.target_behavior({},100,slug,ref,now=start+5)
  self.assertEqual(b['observed_crossings'],1);self.assertEqual(b['observed_seconds'],1)
  self.assertEqual(b['seconds_above'],1);self.assertEqual(b['price'],101)
  self.assertEqual(b['coverage_pct'],20)
  self.assertEqual(r.target_behavior({},None,slug,ref)['status'],'unavailable')
 def test_summary_excludes_fallback_and_pairs_same_checkpoint(self):
  calls={'AI':{'direction':'UP','source':'AI_REVIEW','confidence':'HIGH'},'INDICATORS':{'direction':'WAIT'},'TARGET_SIDE':{'direction':'DOWN'}}
  rows=[('m','HALFWAY','now','{}',json.dumps(calls),'UP')]
  report=r.summarize(rows)
  self.assertEqual(report['confidence'][0]['accuracy_pct'],100)
  self.assertEqual(report['confidence'][0]['checkpoint'],'HALFWAY')
  self.assertEqual(len(report['paired_comparisons']),1)
  self.assertEqual(report['paired_comparisons'][0]['ai_only_correct'],1)
  calls['AI']['source']='INDICATORS_FALLBACK'
  report=r.summarize([('m','HALFWAY','now','{}',json.dumps(calls),'UP')])
  self.assertEqual(report['confidence'],[]);self.assertEqual(report['paired_comparisons'],[])
 def test_news_similarity_and_republishing(self):
  now=time.time();title='Bitcoin ETF approves new fund as crypto investors watch markets'
  items=[{'title':title,'url':'https://a.example/2020/01/01/story','source':'A','published_epoch':now,'published_at':'now'}, {'title':title,'url':'https://b.example/story','source':'B','published_epoch':now-1,'published_at':'now'}]
  chosen=intel.select_headlines(items,now)
  self.assertEqual(len(chosen),1)
  self.assertTrue(chosen[0]['possible_republication'] or 'A' in chosen[0]['also_reported_by'])

class ReferenceTests(unittest.IsolatedAsyncioTestCase):
 async def test_reference_rejects_gapfilled_and_wrong_asset(self):
  slug='cpc-btc-updown-15m-2026-10-25-1400z';start=r.market_start(slug);now=start+450
  candles=[{'timestamp':start-60+i,'close':'100','tickCount':1} for i in range(60)]+[{'timestamp':now-1,'close':'101','tickCount':1},{'timestamp':now,'close':'999','tickCount':0},{'timestamp':now+1,'close':'999','tickCount':1},{'timestamp':now-2,'close':'999','tickCount':'bad'},None]
  c=type('Client',(),{'get':AsyncMock(return_value=Reply({'series':[{'asset':{'symbol':'eth','assetClass':'ASSET_CLASS_CRYPTO'},'currency':'USD','candles':[]},{'asset':{'symbol':'btc','assetClass':'ASSET_CLASS_CRYPTO'},'currency':'USD','candles':candles}]}))})()
  with patch('research.time.time',return_value=now):
   ref=await r.reference_prices(c,{'slug':slug,'assetPriceTerms':{'indexSymbol':'BRTI'}})
  self.assertEqual(ref['price'],101);self.assertEqual(ref['opening_average'],100)
  self.assertEqual(ref['opening_average_samples'],60);self.assertEqual(ref['status'],'fresh')
 async def test_quote_complement_and_market_match(self):
  c=type('Client',(),{'get':AsyncMock(return_value=Reply({'marketData':{'marketSlug':'m','state':'MARKET_STATE_OPEN','bestBid':{'value':'.30'},'bestAsk':{'value':'.35'}}}))})()
  q=await r.market_quote(c,'m');self.assertEqual(q['down_entry'],.7);self.assertEqual(q['up_entry'],.35)
  self.assertEqual((await r.market_quote(c,'other'))['status'],'unavailable')

class PersistenceTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.old=os.getcwd();self.tmp=tempfile.TemporaryDirectory();os.chdir(self.tmp.name)
  self.db=app.DB;app.DB='';app.setup();app._research_cache.update(updated_epoch=0,report=None)
 def tearDown(self):
  app.DB=self.db;os.chdir(self.old);self.tmp.cleanup()
 def fixture(self):
  slug='cpc-btc-updown-15m-2020-01-01-1400z'
  market={'slug':slug,'assetPriceTerms':{'indexSymbol':'BRTI','priceToBeat':{'value':'100'},'marketType':'ASSET_PRICE_MARKET_TYPE_UP_DOWN','asset':{'symbol':'btc'}},'marketSides':[{'long':True,'description':'yes'},{'long':False,'description':'no'}],'status':'MARKET_STATUS_RESOLVED'}
  d={'price':101,'momentum_pct':.1,'long_momentum_pct':.2,'volume_ratio':1,'candle_age_seconds':0,'target_behavior':{'status':'available','side':'UP','source':'reference'},'market_quote':{'status':'available','market':slug,'up_entry':.4,'down_entry':.65},'graph_evidence':{}}
  review={'direction':'UP','source':'AI_REVIEW','ai_confidence':'HIGH','reason':'test','evidence':{},'news_assessment':{'effect':'NONE','headlines':[]}}
  return slug,market,d,review
 async def test_same_snapshot_atomic_idempotent_and_settlement(self):
  slug,market,d,review=self.fixture()
  with patch('app.halfway_decision',new=AsyncMock(return_value=review)) as ai:
   await app.capture_checkpoint(None,slug,'HALFWAY',450,d,'WAIT','WAIT',1,market)
   d['price']=999
   await app.capture_checkpoint(None,slug,'HALFWAY',440,d,'DOWN','WAIT',-1,market)
   self.assertEqual(ai.await_count,1)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM research_calls',fetch=True)[0][0],1)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM signals',fetch=True)[0][0],1)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM ai_decisions',fetch=True)[0][0],1)
  calls=json.loads(app.execute('SELECT calls_json FROM research_calls',fetch=True)[0][0]);self.assertEqual(calls['TARGET_SIDE']['direction'],'UP');self.assertEqual(calls['INDICATORS']['direction'],'WAIT')
  class Client:
   async def get(self,url,**kw):return Reply({'settlement':1}) if url.endswith('/settlement') else Reply({'event':{'markets':[market]}})
  await app.settle(Client());self.assertEqual(app.execute('SELECT winner FROM research_calls',fetch=True)[0][0],'UP')
  report=app.research_report();self.assertEqual(report['confidence'][0]['wins'],1)
 async def test_early_only_call_is_settled_without_legacy_signal(self):
  slug,market,d,review=self.fixture()
  with patch('app.halfway_decision',new=AsyncMock(return_value=review)):await app.capture_checkpoint(None,slug,'EARLY',720,d,'UP','WAIT',1,market)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM signals',fetch=True)[0][0],0)
  class Client:
   async def get(self,url,**kw):return Reply({'settlement':0}) if url.endswith('/settlement') else Reply({'event':{'markets':[market]}})
  await app.settle(Client());self.assertEqual(app.execute('SELECT winner FROM research_calls',fetch=True)[0][0],'DOWN')
 async def test_transaction_rollback(self):
  slug,market,d,review=self.fixture();app.execute('DROP TABLE ai_decisions')
  with patch('app.halfway_decision',new=AsyncMock(return_value=review)):
   with self.assertRaises(Exception):await app.capture_checkpoint(None,slug,'HALFWAY',450,d,'UP','WAIT',1,market)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM research_calls',fetch=True)[0][0],0)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM signals',fetch=True)[0][0],0)
 async def test_final_price_accuracy_retries_after_binary_settlement(self):
  # A binary winner may be verified before official numeric settlement metadata
  # is published. Never substitute the public 1-second chart for that value.
  start=(int(time.time())//900-2)*900
  stamp=datetime.fromtimestamp(start,timezone.utc).strftime('%Y-%m-%d-%H%Mz').lower()
  slug='cpc-btc-updown-15m-'+stamp
  snapshot={'reference_price':{'source':'Polymarket US BTC reference / BRTI'},
            'settlement_forecast':{'current_reference_usd':100}}
  calls={'AI':{'source':'AI_REVIEW','direction':'UP','forecast_final_mean_usd':101},
         'PRICE_PATH':{'direction':'UP','forecast_final_mean_usd':100.7}}
  app.execute('INSERT INTO research_calls (id,market,checkpoint,created_at,snapshot_json,calls_json,winner,final_reference_mean) VALUES (?,?,?,?,?,?,?,?)',
              (slug+':HALFWAY',slug,'HALFWAY',datetime.now(timezone.utc).isoformat(),
               json.dumps(snapshot),json.dumps(calls),'UP',99.2))
  market={'slug':slug,'status':'MARKET_STATUS_RESOLVED','marketSides':[
      {'long':True,'description':'yes'},{'long':False,'description':'no'}],
      'assetPriceTerms':{'marketType':'ASSET_PRICE_MARKET_TYPE_UP_DOWN',
         'indexSymbol':'BRTI','asset':{'symbol':'btc'},'priceToBeat':100,
         'settlementPrice':None}}
  client=type('Client',(),{'get':AsyncMock(return_value=Reply({'event':{'markets':[market]}}))})()
  app._reference_retry_after.pop(slug,None)
  pending=await app.retry_numeric_final_means(client,now=time.time())
  self.assertEqual(pending['graded'],0)
  self.assertEqual(pending['incomplete'],1)
  self.assertEqual(client.get.await_count,1)
  self.assertEqual(app.execute('SELECT final_reference_source FROM research_calls WHERE market=?',(slug,),fetch=True)[0][0],'LEGACY_UNVERIFIED')
  retry_after=app._reference_retry_after[slug]
  market['assetPriceTerms']['settlementPrice']=100.5
  done=await app.retry_numeric_final_means(client,now=retry_after+1)
  self.assertEqual(done['graded'],1)
  self.assertEqual(client.get.await_count,2)
  self.assertEqual(app.execute('SELECT winner FROM research_calls WHERE market=?',(slug,),fetch=True)[0][0],'UP')
  self.assertEqual(app.execute('SELECT final_reference_mean FROM research_calls WHERE market=?',(slug,),fetch=True)[0][0],100.5)
  self.assertEqual(app.execute('SELECT final_reference_source FROM research_calls WHERE market=?',(slug,),fetch=True)[0][0],'OFFICIAL_MARKET_SETTLEMENT')
  report=app.numeric_price_forecast_report()
  results={x['source']:x for x in report['results']}
  self.assertEqual(results['CURRENT_BRTI_BASELINE']['mean_absolute_error_usd'],.5)
  self.assertEqual(results['AI']['mean_absolute_error_usd'],.5)
  self.assertEqual(results['PRICE_PATH']['mean_absolute_error_usd'],.2)
  self.assertEqual(report['paired_comparisons'][0]['paired_rounds'],1)
  self.assertEqual(report['paired_comparisons'][0]['ai_mae_usd'],.5)
  self.assertEqual(report['paired_comparisons'][0]['price_path_mae_usd'],.2)

 async def test_endpoints_and_quote_round_guard(self):
  c=TestClient(app.app);self.assertEqual(c.get('/api/research').status_code,200)
  self.assertEqual(c.get('/api/research.csv').status_code,200)
  self.assertEqual(c.get('/research-dashboard.js').status_code,200)
  app.state.update(market='new',market_quote={'status':'available','market':'old','up_entry':.4,'fetched_epoch':time.time()})
  self.assertEqual(c.get('/api/entry-estimate').json()['status'],'unavailable')
  self.assertEqual(c.get('/api/entry-estimate?contracts=-1').status_code,422)

if __name__=='__main__':unittest.main()
