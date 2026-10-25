import json,os,tempfile,time,unittest,asyncio
from datetime import datetime,timezone
from unittest.mock import patch
import app,paper_trader as p
from fastapi.testclient import TestClient

class BookTests(unittest.TestCase):
 def book(self):return {'market':'m','book_epoch':time.time(),'bids':[(.45,10)],'offers':[(.50,3),(.52,10)],'spread':.05}
 def test_depth_slippage_budget_and_fees(self):
  f=p.entry_fill(self.book(),'UP',1000,p.RISK)
  self.assertEqual(f['quantity'],13);self.assertGreater(f['cost_cents'],3*50+10*52)
  self.assertGreater(f['fee_cents'],0);self.assertAlmostEqual(f['lots'][0][0],.505)
  small=p.entry_fill(self.book(),'UP',200,p.RISK)
  self.assertLessEqual(small['cost_cents'],200);self.assertLess(small['quantity'],13)
 def test_down_uses_yes_bids(self):
  f=p.entry_fill(self.book(),'DOWN',1000,p.RISK)
  self.assertAlmostEqual(f['average_book_price'],.55)
 def test_one_sided_and_spread_skipped(self):
  b=self.book();b['spread']=None;self.assertIsNone(p.entry_fill(b,'UP',1000,p.RISK))
  b['spread']=.2;self.assertIsNone(p.entry_fill(b,'UP',1000,p.RISK))
 def test_stale_book_rejected(self):
  body={'marketData':{'marketSlug':'m','state':'MARKET_STATE_OPEN','transactTime':datetime.fromtimestamp(time.time()-100,timezone.utc).isoformat(),'offers':[],'bids':[]}}
  with self.assertRaises(ValueError):p.normalize_book(body,'m')
 def test_partial_exit_values_missing_units_at_zero(self):
  value,missing=p.exit_mark(self.book(),'UP',15,.005)
  self.assertEqual(missing,5);self.assertGreater(value,0);self.assertLess(value,10*45)

class LedgerTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.cwd=os.getcwd();self.tmp=tempfile.TemporaryDirectory();os.chdir(self.tmp.name)
  self.db=app.DB;app.DB='';app.setup();app.execute("UPDATE paper_account SET started_at='2000-01-01T00:00:00+00:00'")
  self.now=int(time.time()//900)*900+450
  self.slug='cpc-btc-updown-15m-'+datetime.fromtimestamp(self.now-450,timezone.utc).strftime('%Y-%m-%d-%H%Mz')
  self.created=datetime.fromtimestamp(self.now,timezone.utc).isoformat()
  self.row=(self.slug,self.slug+':HALFWAY',self.created,json.dumps({'reference_price':{'status':'fresh'},'official_target':80000}),json.dumps({'AI':{'direction':'UP','source':'AI_REVIEW','confidence':'MEDIUM'}}))
 def tearDown(self):app.DB=self.db;os.chdir(self.cwd);self.tmp.cleanup()
 def client(self):
  now=self.now;slug=self.slug
  class R:
   def raise_for_status(self):pass
   def json(self):return {'marketData':{'marketSlug':slug,'state':'MARKET_STATE_OPEN','transactTime':datetime.fromtimestamp(now,timezone.utc).isoformat(),'bids':[{'px':{'value':'.49'},'qty':'100'}],'offers':[{'px':{'value':'.50'},'qty':'100'}]}}
  class C:
   async def get(self,url,**kw):
    if '/book' not in url:raise AssertionError('Unexpected network request')
    return R()
  return C()
 async def enter(self,row=None):
  with patch('paper_trader.time.time',return_value=self.now):await p.try_entry(self.client(),app.execute,app.connect,False,row or self.row)
 async def test_one_entry_and_cash_debit_atomic(self):
  await self.enter();await self.enter()
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],1)
  cost=app.execute('SELECT cost_cents FROM paper_positions',fetch=True)[0][0]
  self.assertLessEqual(cost,1000);self.assertEqual(app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0],100000-cost)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_attempts',fetch=True)[0][0],1)
 async def test_pause_persists_and_does_not_debit(self):
  p.control(app.connect,False,'pause');await self.enter()
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
  self.assertEqual(app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0],100000)
  self.assertEqual(app.execute('SELECT reason FROM paper_attempts',fetch=True)[0][0],'Paper trading paused')
  p.control(app.connect,False,'resume');await self.enter();self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0) # No backfill of skipped call.
 async def test_low_confidence_skipped(self):
  row=list(self.row);row[4]=json.dumps({'AI':{'direction':'UP','source':'AI_REVIEW','confidence':'LOW'}})
  await self.enter(tuple(row));self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
 async def test_daily_budget_reserves_open_risk(self):
  risk=dict(p.RISK,daily_loss_cents=25);app.execute('UPDATE paper_account SET risk_json=?',(json.dumps(risk),))
  await self.enter();self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
 async def test_settlement_only_once_even_when_paused(self):
  await self.enter();quantity,cost=app.execute('SELECT quantity,cost_cents FROM paper_positions',fetch=True)[0]
  app.execute('INSERT INTO research_calls (id,market,checkpoint,created_at,snapshot_json,calls_json,winner) VALUES (?,?,?,?,?,?,?)',(self.row[1],self.slug,'HALFWAY',self.created,self.row[3],self.row[4],'UP'))
  p.control(app.connect,False,'pause');p.settle_positions(app.connect,False);p.settle_positions(app.connect,False)
  self.assertEqual(app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0],100000-cost+quantity*100)
  self.assertEqual(app.execute('SELECT pnl_cents FROM paper_positions',fetch=True)[0][0],quantity*100-cost)
  report=p.report(app.execute);self.assertFalse(report['real_orders_enabled']);self.assertEqual(report['settled_trades'],1)
 def exit_client(self,bid=.62,ask=.63,quantity=100):
  now=self.now;slug=self.slug
  class R:
   def raise_for_status(self):pass
   def json(self):return {'marketData':{'marketSlug':slug,'state':'MARKET_STATE_OPEN','transactTime':datetime.fromtimestamp(now,timezone.utc).isoformat(),'bids':[{'px':{'value':str(bid)},'qty':str(quantity)}],'offers':[{'px':{'value':str(ask)},'qty':'100'}]}}
  class C:
   async def get(self,url,**kw):return R()
  return C()
 async def test_take_profit_sells_and_cancels_other_orders(self):
  await self.enter()
  with patch('paper_trader.time.time',return_value=self.now):await p.manage_exits(self.exit_client(),app.execute,app.connect,False)
  status,reason,pnl=app.execute('SELECT status,exit_reason,pnl_cents FROM paper_positions',fetch=True)[0]
  self.assertEqual((status,reason),('CLOSED','TAKE_PROFIT'));self.assertGreater(pnl,0)
  orders=dict(app.execute('SELECT kind,status FROM paper_orders',fetch=True));self.assertEqual(orders['TAKE_PROFIT'],'FILLED');self.assertEqual(orders['STOP_LOSS'],'CANCELLED')
  cash=app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0]
  p.settle_positions(app.connect,False);self.assertEqual(app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0],cash)
 async def test_stop_loss_sells(self):
  await self.enter()
  with patch('paper_trader.time.time',return_value=self.now):await p.manage_exits(self.exit_client(.35,.36),app.execute,app.connect,False)
  reason,pnl=app.execute('SELECT exit_reason,pnl_cents FROM paper_positions',fetch=True)[0]
  self.assertEqual(reason,'STOP_LOSS');self.assertLess(pnl,0)
 async def test_unfilled_time_exit_is_flagged_not_fabricated(self):
  await self.enter();cash=app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0]
  app.execute("UPDATE paper_orders SET threshold=? WHERE kind='TIME_EXIT'",(self.now-1,))
  with patch('paper_trader.time.time',return_value=self.now):await p.manage_exits(self.exit_client(.49,.50,1),app.execute,app.connect,False)
  self.assertEqual(app.execute('SELECT status FROM paper_positions',fetch=True)[0][0],'OPEN')
  self.assertEqual(app.execute("SELECT status FROM paper_orders WHERE kind='TIME_EXIT'",fetch=True)[0][0],'UNFILLED')
  self.assertEqual(app.execute('SELECT cash_cents FROM paper_account',fetch=True)[0][0],cash)
 async def test_emergency_stop_exits_even_when_entries_paused(self):
  await self.enter()
  with patch('paper_trader.time.time',return_value=self.now):
   p.control(app.connect,False,'stop')
   await p.manage_exits(self.exit_client(.49,.50),app.execute,app.connect,False)
  self.assertEqual(app.execute('SELECT paused FROM paper_account',fetch=True)[0][0],1)
  self.assertEqual(app.execute('SELECT status,exit_reason FROM paper_positions',fetch=True)[0],('CLOSED','TIME_EXIT'))
 def live_signal(self,ident='live-1',max_entry=.76,target=.91,action='ENTER_UP'):
  return {'status':'ready','market':self.slug,'paper_signal_id':ident,'captured_epoch':self.now,
          'snapshot':{'reference_price':{'status':'fresh'},'official_target':80000,'checkpoint':'EARLY'},
          'profit_opportunity':{'action':action,'confidence':'HIGH','max_entry_price':max_entry,'target_exit_bid':target,'position_action':'HOLD','reason':'test profit case'}}
 async def test_live_entry_does_not_wait_for_halfway(self):
  self.now=self.now-330 # Two minutes into the round, not halfway.
  with patch('paper_trader.time.time',return_value=self.now):await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal())
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],1)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM research_calls',fetch=True)[0][0],0)
 async def test_new_prediction_can_reconsider_skipped_market(self):
  with patch('paper_trader.time.time',return_value=self.now):
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal('too-expensive',max_entry=.74))
   self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal('good-entry'))
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal('good-entry'))
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],1)
 async def test_profit_target_must_cover_fees(self):
  with patch('paper_trader.time.time',return_value=self.now):await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal(target=.76))
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
  self.assertIn('net profit',app.execute('SELECT reason FROM paper_attempts',fetch=True)[0][0])
 async def test_low_live_buy_uses_smaller_budget_for_existing_account(self):
  app.execute('UPDATE paper_account SET risk_json=?',(json.dumps({'min_confidence':'MEDIUM','max_trade_cents':1000}),))
  signal=self.live_signal();signal['profit_opportunity']['confidence']='LOW'
  with patch('paper_trader.time.time',return_value=self.now):
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,signal)
  cost=app.execute('SELECT cost_cents FROM paper_positions',fetch=True)[0][0]
  self.assertGreater(cost,0);self.assertLessEqual(cost,500)
  self.assertEqual(p.report(app.execute)['risk']['min_confidence'],'LOW')
 async def test_missing_confidence_does_not_become_low_buy(self):
  signal=self.live_signal();del signal['profit_opportunity']['confidence']
  with patch('paper_trader.time.time',return_value=self.now):
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,signal)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
 async def test_low_buy_still_requires_net_profit(self):
  signal=self.live_signal(target=.76);signal['profit_opportunity']['confidence']='LOW'
  with patch('paper_trader.time.time',return_value=self.now):
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,signal)
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
 async def test_smaller_opportunity_enters_on_existing_account_and_takes_profit(self):
  risk=dict(p.RISK,take_profit_pct=.10)
  app.execute('UPDATE paper_account SET risk_json=?',(json.dumps(risk),))
  with patch('paper_trader.time.time',return_value=self.now):
   await p.cycle(self.exit_client(.74,.75),app.execute,app.connect,False,self.live_signal(target=.83))
   self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],1)
   await p.manage_exits(self.exit_client(.83,.84),app.execute,app.connect,False)
  status,pnl,cost=app.execute('SELECT status,pnl_cents,cost_cents FROM paper_positions',fetch=True)[0]
  self.assertEqual(status,'CLOSED');self.assertGreaterEqual(pnl/cost,.05);self.assertLess(pnl/cost,.10)
  self.assertEqual(p.report(app.execute)['risk']['take_profit_pct'],.05)
 async def test_cap_applies_to_existing_account_and_all_in_cost(self):
  risk=dict(p.RISK,max_price=.99,max_all_in_price=.99);app.execute('UPDATE paper_account SET risk_json=?',(json.dumps(risk),))
  self.assertEqual(p.report(app.execute)['risk']['max_price'],.85)
  with patch('paper_trader.time.time',return_value=self.now):await p.cycle(self.exit_client(.83,.84),app.execute,app.connect,False,self.live_signal(max_entry=.84,target=.99))
  self.assertEqual(app.execute('SELECT COUNT(*) FROM paper_positions',fetch=True)[0][0],0)
  b={'bids':[(.84,100)],'offers':[(.85,100)],'book_epoch':self.now,'spread':.01}
  self.assertIsNone(p.entry_fill(b,'UP',1000,p.risk_settings(risk)))
 async def test_ai_can_take_positive_profit_before_fixed_target(self):
  await self.enter();ident=app.execute('SELECT id FROM paper_positions',fetch=True)[0][0]
  signal={'status':'ready','captured_epoch':self.now,'snapshot':{'paper_position':{'position_id':ident}},'profit_opportunity':{'position_action':'TAKE_PROFIT'}}
  with patch('paper_trader.time.time',return_value=self.now):await p.manage_exits(self.exit_client(.55,.56),app.execute,app.connect,False,signal)
  status,pnl,cost=app.execute('SELECT status,pnl_cents,cost_cents FROM paper_positions',fetch=True)[0]
  self.assertEqual(status,'CLOSED');self.assertGreater(pnl,0);self.assertLess(pnl/cost,.10)
 async def test_control_requires_same_origin_and_no_real_action(self):
  c=TestClient(app.app)
  self.assertEqual(c.post('/api/paper/control',json={'action':'pause'}).status_code,403)
  headers={'Origin':'http://testserver','X-Paper-Control':'1'}
  self.assertEqual(c.post('/api/paper/control',json={'action':'enable_real_trading'},headers=headers).status_code,400)
  self.assertEqual(c.post('/api/paper/control',json={'action':'pause'},headers=headers).status_code,200)
  self.assertEqual(c.get('/api/paper').json()['status'],'paused')

if __name__=='__main__':unittest.main()

