"""Shared fake-money sandbox. GET-only exchange access; no real-order capability."""
import asyncio, json, math, os, time, uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo
from research import number, market_start, FEE_THETA
import paper_learning as learning

INITIAL_CENTS=100000
RISK={'max_trade_cents':1000,'daily_loss_cents':2500,'max_open_cents':5000,
      'min_price':.10,'max_price':.85,'max_all_in_price':.85,'max_spread':.08,'slippage':.005,
      'take_profit_pct':.08,'stretch_profit_pct':.15,'profit_trail_pct':.025,'stop_loss_pct':.08,'max_hold_seconds':180,'exit_before_expiry_seconds':180,
      'min_confidence':'LOW','low_confidence_max_trade_cents':500,'checkpoint':'LIVE_ANYTIME','daily_timezone':'America/Chicago'}
_last_mark=0.0
_last_entry_scan=0.0
_last_settlement_scan=0.0
_profit_peaks={}  # Observed executable net ROI, not contract midpoint.

def risk_settings(stored):
    risk={**RISK,**stored}
    risk['max_price']=min(float(risk['max_price']),.85)
    risk['max_all_in_price']=min(float(risk.get('max_all_in_price',.85)),.85)
    risk['checkpoint']='LIVE_ANYTIME'
    risk['min_confidence']='LOW'
    risk['low_confidence_max_trade_cents']=min(int(risk['low_confidence_max_trade_cents']),500)
    # Apply the faster profit strategy to existing accounts without resetting their ledger.
    risk['take_profit_pct']=RISK['take_profit_pct']
    return risk

def stamp():return datetime.now(timezone.utc).isoformat()
def day_bounds():
    now=datetime.now(ZoneInfo('America/Chicago'));start=now.replace(hour=0,minute=0,second=0,microsecond=0)
    return start.astimezone(timezone.utc).isoformat(),(start+timedelta(days=1)).astimezone(timezone.utc).isoformat()

def setup(execute):
    execute('''CREATE TABLE IF NOT EXISTS paper_account (
      id TEXT PRIMARY KEY, initial_cents INTEGER NOT NULL,cash_cents INTEGER NOT NULL,
      paused INTEGER NOT NULL,started_at TEXT NOT NULL,risk_json TEXT NOT NULL,
      high_water_cents INTEGER NOT NULL,max_drawdown_cents INTEGER NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS paper_positions (
      id TEXT PRIMARY KEY,market TEXT NOT NULL UNIQUE,call_id TEXT NOT NULL,direction TEXT NOT NULL,
      quantity INTEGER NOT NULL,cost_cents INTEGER NOT NULL,fee_cents INTEGER NOT NULL,
      status TEXT NOT NULL,created_at TEXT NOT NULL,settled_at TEXT,winner TEXT,payout_cents INTEGER,
      pnl_cents INTEGER,exit_reason TEXT,exit_net_cents INTEGER,exit_fee_cents INTEGER,closed_at TEXT,mark_cents INTEGER NOT NULL,mark_epoch REAL,evidence_json TEXT NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS paper_attempts (
      market TEXT PRIMARY KEY,created_at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,detail_json TEXT NOT NULL)''')
    execute('''CREATE TABLE IF NOT EXISTS paper_equity (
      id TEXT PRIMARY KEY,created_at TEXT NOT NULL,equity_cents INTEGER NOT NULL,
      cash_cents INTEGER NOT NULL,open_mark_cents INTEGER NOT NULL)''')
    for col in ('exit_reason TEXT','exit_net_cents INTEGER','exit_fee_cents INTEGER','closed_at TEXT'):
        try:execute('ALTER TABLE paper_positions ADD COLUMN '+col)
        except Exception as exc:
            if 'duplicate column' not in str(exc).lower() and 'already exists' not in str(exc).lower():raise
    execute('''CREATE TABLE IF NOT EXISTS paper_orders (
      id TEXT PRIMARY KEY,position_id TEXT NOT NULL,market TEXT NOT NULL,kind TEXT NOT NULL,
      status TEXT NOT NULL,threshold REAL NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
      detail TEXT NOT NULL,UNIQUE(position_id,kind))''')
    execute('''CREATE TABLE IF NOT EXISTS paper_live_calls (
      id TEXT PRIMARY KEY,market TEXT NOT NULL,created_at TEXT NOT NULL,snapshot_json TEXT NOT NULL,
      calls_json TEXT NOT NULL,processed INTEGER NOT NULL DEFAULT 0)''')
    execute("""CREATE TABLE IF NOT EXISTS paper_missed_opportunities (
      id TEXT PRIMARY KEY, market TEXT NOT NULL, direction TEXT NOT NULL,
      observed_epoch REAL NOT NULL, entry_ask REAL NOT NULL, quantity INTEGER NOT NULL,
      entry_cost_cents INTEGER NOT NULL, status TEXT NOT NULL,
      best_exit_bid REAL, best_exit_epoch REAL, best_net_cents INTEGER,
      max_roi_pct REAL NOT NULL DEFAULT 0, resolved_epoch REAL)""")
    execute("CREATE INDEX IF NOT EXISTS missed_market_status ON paper_missed_opportunities (market,status)")
    learning.setup(execute)
    execute('INSERT INTO paper_account (id,initial_cents,cash_cents,paused,started_at,risk_json,high_water_cents,max_drawdown_cents) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
            ('main',INITIAL_CENTS,INITIAL_CENTS,0,stamp(),json.dumps(RISK),INITIAL_CENTS,0))

def locked_account(c,postgres):
    cur=c.cursor()
    if not postgres:cur.execute('BEGIN IMMEDIATE')
    cur.execute('SELECT initial_cents,cash_cents,paused,started_at,risk_json,high_water_cents,max_drawdown_cents FROM paper_account WHERE id=\'main\''+(' FOR UPDATE' if postgres else ''))
    row=cur.fetchone()
    if row is None:raise ValueError('Paper account missing')
    return cur,row

def run(cur,postgres,sql,args=()):return cur.execute(sql.replace('?','%s') if postgres else sql,args)

def cost_lots(lots):
    exact=Decimal(0);collected=Decimal(0);cost=Decimal(0)
    for p,q in lots:
        p=Decimal(str(p));q=Decimal(q);fee=Decimal(str(FEE_THETA))*q*p*(1-p)
        exact+=fee;rounded=fee.quantize(Decimal('.01'),rounding=ROUND_HALF_EVEN)
        charge=max(Decimal(0),min(rounded,exact.quantize(Decimal('.01'),rounding=ROUND_HALF_EVEN)-collected))
        collected+=charge;cost+=p*q
    # Reserve fractional cents upward, so debits never exceed the available budget.
    from decimal import ROUND_CEILING
    return int(((cost+collected)*100).quantize(Decimal('1'),rounding=ROUND_CEILING)),int(collected*100)

def normalize_book(body,slug,now=None):
    now=time.time() if now is None else now;m=body.get('marketData') or {}
    if m.get('marketSlug')!=slug or m.get('state')!='MARKET_STATE_OPEN':raise ValueError('Book is not open for this contract')
    try:book_time=datetime.fromisoformat(m['transactTime'].replace('Z','+00:00')).timestamp()
    except (KeyError,ValueError,TypeError):raise ValueError('Book timestamp unavailable')
    if not -2<=now-book_time<=30:raise ValueError('Book timestamp is stale')
    def levels(side):
        out=[]
        for x in m.get(side) or []:
            p=number(x.get('px'));q=number(x.get('qty'))
            if p is not None and q is not None and 0<p<1 and q>=1:out.append((p,int(q)))
        # Combine duplicate levels and keep whole-contract liquidity only.
        sums={}
        for p,q in out:sums[p]=sums.get(p,0)+q
        return sorted(sums.items(),reverse=side=='bids')
    bids=levels('bids');offers=levels('offers')
    if bids and offers and bids[0][0]>offers[0][0]:raise ValueError('Crossed book')
    return {'market':slug,'bids':bids,'offers':offers,'book_epoch':book_time,'received_epoch':now,
            'spread':round(offers[0][0]-bids[0][0],6) if bids and offers else None}

async def fetch_book(client,slug):
    response=await client.get('https://gateway.polymarket.us/v1/markets/'+slug+'/book',timeout=6)
    response.raise_for_status()
    return normalize_book(response.json(),slug)

def entry_fill(book,direction,budget_cents,risk):
    if direction not in ('UP','DOWN') or budget_cents<=0:return None
    if book.get('spread') is None or book['spread']>risk['max_spread']:return None
    levels=book['offers'] if direction=='UP' else sorted([(round(1-p,8),q) for p,q in book['bids']])
    lots=[];quoted=[]
    for p,available in levels:
        fill=float(Decimal(str(p))+Decimal(str(risk['slippage'])))
        if fill<risk['min_price']:continue
        if fill>=risk['max_price']:break
        low=0;high=min(available,budget_cents) # At least one cent per contract.
        while low<high:
            mid=(low+high+1)//2
            if cost_lots(lots+[(fill,mid)])[0]<=budget_cents:low=mid
            else:high=mid-1
        if low:
            lots.append((fill,low));quoted.append((p,low))
        if low<available:break
    if not lots:return None
    quantity=sum(q for p,q in lots);cost,fee=cost_lots(lots)
    if cost/(100*quantity)>=min(risk.get('max_all_in_price',.85),.85):return None
    return {'quantity':quantity,'cost_cents':cost,'fee_cents':fee,
            'average_assumed_fill':sum(p*q for p,q in lots)/quantity,
            'average_book_price':sum(p*q for p,q in quoted)/quantity,
            'lots':lots,'quoted_lots':quoted,'book_epoch':book['book_epoch'],
            'note':'Simulated immediate depth fill with adverse slippage and US taker fees. No actual order or guaranteed fill.'}

def exit_mark(book,direction,quantity,slippage):
    levels=book['bids'] if direction=='UP' else sorted([(round(1-p,8),q) for p,q in book['offers']],reverse=True)
    remaining=quantity;lots=[]
    for p,available in levels:
        q=min(available,remaining);p=max(0,float(Decimal(str(p))-Decimal(str(slippage))))
        if q:lots.append((p,q));remaining-=q
        if not remaining:break
    gross=sum(Decimal(str(p))*q for p,q in lots)
    _,fee=cost_lots(lots)
    from decimal import ROUND_FLOOR
    return max(0,int((gross*100).quantize(Decimal('1'),rounding=ROUND_FLOOR))-fee),remaining

def gross_daily_losses(cur,postgres):
    a,b=day_bounds()
    run(cur,postgres,'SELECT COALESCE(SUM(CASE WHEN pnl_cents<0 THEN -pnl_cents ELSE 0 END),0) FROM paper_positions WHERE status IN (\'SETTLED\',\'CLOSED\') AND COALESCE(closed_at,settled_at)>=? AND COALESCE(closed_at,settled_at)<?',(a,b))
    return int(cur.fetchone()[0])

def record_attempt(cur,postgres,market,status,reason,detail):
    run(cur,postgres,'INSERT INTO paper_attempts (market,created_at,status,reason,detail_json) VALUES (?,?,?,?,?) ON CONFLICT(market) DO UPDATE SET created_at=excluded.created_at,status=excluded.status,reason=excluded.reason,detail_json=excluded.detail_json',(market,stamp(),status,reason,json.dumps(detail)))

def dip_rebound_check(snapshot, call):
    """Paper-only UP dip entries: require observed pullback AND early rebound.
    This is a heuristic, not a forecast or a claim of profitable execution."""
    if call.get('direction')!='UP':
        return True, 'Not an UP dip entry'
    chart=snapshot.get('chart_pattern') or {}
    graph=snapshot.get('graph_evidence') or {}
    candles=graph.get('candles_ohlcv') or []
    if len(candles)<8:
        return False, 'Dip watch: insufficient recent candles'
    try:
        closes=[float(x[4]) for x in candles[-12:]]
        highs=[float(x[2]) for x in candles[-12:]]
        lows=[float(x[3]) for x in candles[-12:]]
        if min(closes)<=0:return False,'Dip watch: invalid candle prices'
        # Pullback must have occurred recently, not just a continually rising price.
        recent_peak=max(highs[:-2])
        drawdown=(recent_peak-min(lows[-6:]))/recent_peak
        rebound=(closes[-1]-min(lows[-4:]))/min(lows[-4:])
        higher_close=closes[-1]>closes[-2]
        if drawdown<0.00025:return False,'Dip watch: no meaningful recent pullback'
        if not higher_close or rebound<0.00008:
            return False,'Dip watch: waiting for price to start rebounding'
        if drawdown>0.006:
            return False,'Dip watch: unusually large drop; avoid catching a falling knife'
        return True,'Observed pullback and initial rebound'
    except (TypeError,ValueError,IndexError,ZeroDivisionError):
        return False,'Dip watch: malformed candle evidence'

def observe_quote_opportunities(execute,quote):
    """Forward-track missed paper-entry scenarios from actual sampled US BBO quotes.
    These are top-of-book what-ifs, NOT demonstrated executable trades or earned profit."""
    learning.observe(execute,quote)
    if quote.get('status')!='available' or 'btc-updown-15m-' not in str(quote.get('market','')):
        return
    market=quote['market'];start=market_start(market);now=time.time()
    if start is None or now-start>900:return
    if now-float(quote.get('fetched_epoch') or 0)>15:return
    spread=number(quote.get('spread'))
    if spread is None or spread>RISK['max_spread']:return
    exit_deadline=start+900-RISK['exit_before_expiry_seconds']
    for ident,direction,observed,ask,qty,cost,old_peak in execute(
        "SELECT id,direction,observed_epoch,entry_ask,quantity,entry_cost_cents,max_roi_pct "
        "FROM paper_missed_opportunities WHERE market=? AND status='WATCHING' LIMIT 150",(market,),fetch=True):
        bid=number(quote.get('bid' if direction=='UP' else 'ask'))
        bid=(1-bid) if direction=='DOWN' and bid is not None else bid
        if bid is not None and 0<bid<1 and now>observed:
            net,_=exit_mark({'bids':[(bid,qty)],'offers':[]},'UP',qty,RISK['slippage'])
            roi=round(100*(net-cost)/cost,2)
            if roi>old_peak:
                execute("UPDATE paper_missed_opportunities SET best_exit_bid=?,best_exit_epoch=?,best_net_cents=?,max_roi_pct=? WHERE id=?",
                        (bid,now,net,roi,ident))
            if roi>=100*RISK['take_profit_pct']:
                traded=execute("SELECT 1 FROM paper_positions WHERE market=? AND direction=? AND created_at>=? AND created_at<=? LIMIT 1",
                               (market,direction,datetime.fromtimestamp(observed,timezone.utc).isoformat(),stamp()),fetch=True)
                execute("UPDATE paper_missed_opportunities SET status=?,resolved_epoch=? WHERE id=?",
                        ('MATCHED_PAPER_TRADE' if traded else 'MISSED_QUOTED_PROFIT',now,ident))
                continue
        if now>=min(observed+RISK['max_hold_seconds'],exit_deadline):
            execute("UPDATE paper_missed_opportunities SET status='EXPIRED_NO_TARGET',resolved_epoch=? WHERE id=?",(now,ident))
    if now>=exit_deadline-30:return
    for direction in ('UP','DOWN'):
        ask=number(quote.get('up_entry' if direction=='UP' else 'down_entry'))
        if ask is None or ask<RISK['min_price'] or ask+RISK['slippage']>=RISK['max_price']:continue
        qty=10
        cost,_=cost_lots([(ask+RISK['slippage'],qty)])
        if cost/(100*qty)>=RISK['max_all_in_price']:continue
        # Limit hypothetical candidates to one per direction per 20 seconds.
        existing=execute("SELECT 1 FROM paper_missed_opportunities WHERE market=? AND direction=? AND observed_epoch>? LIMIT 1",
                         (market,direction,now-20),fetch=True)
        if existing:continue
        held=execute("SELECT 1 FROM paper_positions WHERE market=? LIMIT 1",(market,),fetch=True)
        if held:continue
        ident=str(uuid.uuid4())
        execute("INSERT INTO paper_missed_opportunities "
                "(id,market,direction,observed_epoch,entry_ask,quantity,entry_cost_cents,status) VALUES (?,?,?,?,?,?,?,?)",
                (ident,market,direction,now,ask,qty,cost,'WATCHING'))


def polymarket_dip_signal(execute,signal):
    """Extra paper-only dip/rebound entry: fresh AI directional forecast + observed
    US contract odds. The AI has NOT approved the rule-based entry itself."""
    if signal.get('status')!='ready':return None
    age=time.time()-float(signal.get('captured_epoch') or 0)
    if not 0<=age<=60:return None
    market=signal.get('market');direction=signal.get('verdict')
    if direction not in ('UP','DOWN') or 'btc-updown-15m-' not in str(market):return None
    opp=signal.get('profit_opportunity') or {}
    if opp.get('action') in ('ENTER_UP','ENTER_DOWN'):return None
    quote=signal.get('live_quote') or {}
    if quote.get('status')!='available' or quote.get('market')!=market or time.time()-float(quote.get('fetched_epoch') or 0)>12:return None
    snapshot=signal.get('snapshot') or {}
    if (snapshot.get('reference_price') or {}).get('status')!='fresh' or number(snapshot.get('official_target')) is None:return None
    if market_start(market) is None or market_start(market)+900-time.time()<RISK['exit_before_expiry_seconds']+30:return None
    confidence=signal.get('confidence')
    if confidence not in ('LOW','MEDIUM','HIGH'):return None
    if confidence=='LOW' and number(quote.get('up_entry' if direction=='UP' else 'down_entry')) is not None and number(quote.get('up_entry' if direction=='UP' else 'down_entry'))>0.4:return None
    rows=execute("SELECT sampled_epoch,up_ask,down_ask FROM polymarket_quote_samples "
                 "WHERE market=? AND sampled_epoch>=? ORDER BY sampled_epoch DESC LIMIT 25",
                 (market,time.time()-150),fetch=True)
    series=[(t,number(up if direction=='UP' else down)) for t,up,down in reversed(rows)]
    series=[(t,p) for t,p in series if p is not None and 0<p<1]
    if len(series)<5 or series[-1][0]-series[0][0]<20:return None
    previous=[p for t,p in series[:-2]];recent=[p for t,p in series[-7:]]
    if not previous or not recent:return None
    price=number(quote.get('up_entry' if direction=='UP' else 'down_entry'))
    learned_policy=learning.policy(execute,direction,price) if price is not None else {'enabled':False}
    learned=learned_policy['enabled']
    if price is None or not .10<=price<=(.75 if learned else .70):return None
    high=max(previous);low=min(recent)
    if high-low<(max(.020,high*.04) if learned else max(.025,high*.05)):return None
    if not .005<=price-low<=(.055 if learned else .045) or price>high-.012:return None
    if series[-1][1]<series[-2][1]:return None
    if number(quote.get('spread')) is None or quote['spread']>.06:return None
    # Use a stricter entry than the default 85% ceiling for speculative dip buys.
    hypothetical_exit=min(.97,price+max(.06,price*.15))
    captured=time.time()
    call={'direction':direction,'source':'AI_DIRECTION_DIP_RULE','source_mode':'DIP_WATCH',
          'profit_action':'RULE_BASED_DIP_REBOUND','confidence':confidence,
          'reason':'AI '+direction+' direction with observed Polymarket odds dip and early rebound; hypothetical exit, not an AI trade recommendation',
          'max_entry_price':round(price+.008,5),'target_exit_bid':round(hypothetical_exit,5)}
    return (market+':DIP:'+str(int(captured//10)),market,datetime.fromtimestamp(captured,timezone.utc).isoformat(),
            json.dumps({**snapshot,'market_quote':quote,'dip_evidence':{'recent_high':high,'recent_low':low,'entry_ask':price,'samples':len(series),
                           'trigger':'Polymarket 15m contract odds rebound','learned_policy':learned_policy}}),json.dumps({'AI':call}))

async def try_entry(client,execute,connect,postgres,row):
    market,call_id,created,snapshot_json,calls_json=row
    snapshot=json.loads(snapshot_json);call=(json.loads(calls_json).get('AI') or {})
    direction=call.get('direction');reason=None;book=None
    age=time.time()-datetime.fromisoformat(created).timestamp()
    # Cheap preflight prevents repeated market-book requests after a fill or during
    # a skipped-entry cooldown; leave the locked check below as the final guard.
    existing=execute('SELECT status,created_at,detail_json FROM paper_attempts WHERE market=?',(market,),fetch=True)
    if existing:
        p_status,p_at,p_detail=existing[0]
        if p_status=='FILLED':return
        if json.loads(p_detail).get('call_id')==call_id and time.time()-datetime.fromisoformat(p_at).timestamp()<4:return
    if call.get('source') not in ('AI_REVIEW','AI_DIRECTION_DIP_RULE'):reason='No AI directional review or authorized paper-only market trigger'
    elif call.get('source_mode')=='LIVE' and call.get('profit_action') not in ('ENTER_UP','ENTER_DOWN'):reason='Live AI sees no near-term profit opportunity'
    elif direction not in ('UP','DOWN'):reason='No actionable directional call'
    elif call.get('confidence') not in ('MEDIUM','HIGH') and not (call.get('source_mode') in ('LIVE','DIP_WATCH') and call.get('confidence')=='LOW'):reason='AI confidence below paper-test minimum'
    if reason is None and not 0<=age<=60:reason='Saved call is too old for a new paper entry'
    if reason is None and (snapshot.get('reference_price') or {}).get('status')!='fresh':reason='No fresh reference-price evidence at decision time'
    if reason is None and number(snapshot.get('official_target')) is None:reason='Official target unavailable'
    if reason is None and (market_start(market) is None or market_start(market)+900-time.time()<RISK['exit_before_expiry_seconds']+30):reason='Too close to contract expiry'
    if reason is None:
        try:book=await fetch_book(client,market)
        except Exception as exc:reason='Book unavailable: '+type(exc).__name__+': '+str(exc)[:90]
    with connect() as c:
        cur,account=locked_account(c,postgres)
        try:
            run(cur,postgres,'SELECT status,created_at,detail_json FROM paper_attempts WHERE market=?',(market,))
            prior=cur.fetchone()
            if prior and prior[0]=='FILLED':return
            if prior and json.loads(prior[2]).get('call_id')==call_id:
                last_try=time.time()-datetime.fromisoformat(prior[1]).timestamp()
                if last_try<4:return
            def attempt(status,why,detail):record_attempt(cur,postgres,market,status,why,{'call_id':call_id,**detail})
            initial,cash,paused,started,risk_json,high,drawdown=account;risk=risk_settings(json.loads(risk_json))
            if paused or os.getenv('PAPER_TRADING_ENABLED','true').lower() not in ('true','1','yes'):
                attempt('SKIPPED','Paper trading paused',{});return
            if created<started:return
            if reason is None and time.time()-datetime.fromisoformat(created).timestamp()>60:reason='Saved call became stale before simulated entry'
            if reason is None and (time.time()-book['book_epoch']>30 or market_start(market)+900-time.time()<RISK['exit_before_expiry_seconds']+30):reason='Book or contract became ineligible before entry'
            if reason:attempt('SKIPPED',reason,{'call_id':call_id});return
            run(cur,postgres,'SELECT COALESCE(SUM(cost_cents),0) FROM paper_positions WHERE status=\'OPEN\'')
            opened=int(cur.fetchone()[0]);daily=gross_daily_losses(cur,postgres)
            budget=min(cash,risk['max_trade_cents'],risk['max_open_cents']-opened,risk['daily_loss_cents']-daily-opened)
            if call.get('confidence')=='LOW':budget=min(budget,risk['low_confidence_max_trade_cents'])
            if budget<=0:attempt('SKIPPED','Cash, open-risk or daily-loss budget reached',{'open_risk_cents':opened,'daily_realized_losses_cents':daily});return
            fill=entry_fill(book,direction,budget,risk)
            if fill and fill['cost_cents']/(100*fill['quantity'])>=risk['max_all_in_price']:
                fill=None;reason='Entry odds or all-in cost reaches the strict 85 percent cap'
            if fill and call.get('source_mode') in ('LIVE','DIP_WATCH'):
                max_entry=number(call.get('max_entry_price'));target_bid=number(call.get('target_exit_bid'))
                target_book={'bids':[(target_bid,fill['quantity'])] if target_bid is not None else [],'offers':[(1-target_bid,fill['quantity'])] if target_bid is not None else []}
                projected=full_exit(target_book,'UP',fill['quantity'],risk['slippage']) if target_bid is not None and 0<target_bid<1 else None
                immediate=full_exit(book,direction,fill['quantity'],risk['slippage'])
                if max_entry is None or any(px>max_entry for px,q in fill['quoted_lots']):fill=None;reason='Current entry exceeds AI maximum entry price'
                elif not projected or projected['net_cents']<fill['cost_cents']*(1+risk['take_profit_pct']):fill=None;reason='AI exit target does not cover required net profit after fees/slippage'
                elif not immediate or immediate['net_cents']<=fill['cost_cents']*(1-risk['stop_loss_pct']):fill=None;reason='Spread/depth and fees would already breach the stop-loss limit'
            if not fill:attempt('SKIPPED',reason or 'No eligible depth within entry price, spread and risk limits',{'budget_cents':budget,'spread':book.get('spread')});return
            evidence={'model_call':call,'decision_snapshot':snapshot,'entry':fill,'risk':risk,'book':book,'entry_delay_seconds':round(age,2)}
            position_id=str(uuid.uuid4())
            run(cur,postgres,'INSERT INTO paper_positions (id,market,call_id,direction,quantity,cost_cents,fee_cents,status,created_at,mark_cents,mark_epoch,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (position_id,market,call_id,direction,fill['quantity'],fill['cost_cents'],fill['fee_cents'],'OPEN',stamp(),0,None,json.dumps(evidence)))
            for kind,threshold in [('TAKE_PROFIT',risk['take_profit_pct']),('STOP_LOSS',risk['stop_loss_pct']),('TIME_EXIT',min(time.time()+risk['max_hold_seconds'],market_start(market)+900-risk['exit_before_expiry_seconds']))]:
                run(cur,postgres,'INSERT INTO paper_orders (id,position_id,market,kind,status,threshold,created_at,updated_at,detail) VALUES (?,?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()),position_id,market,kind,'WAITING',threshold,stamp(),stamp(),'Simulated protective sell; full fill required, no real order'))
            run(cur,postgres,'UPDATE paper_account SET cash_cents=cash_cents-? WHERE id=\'main\'',(fill['cost_cents'],))
            attempt('FILLED','Simulated depth fill',{'call_id':call_id,**fill})
        finally:cur.close()

def settle_positions(connect,postgres):
    with connect() as c:
        cur,account=locked_account(c,postgres)
        try:
            run(cur,postgres,"SELECT p.id,p.quantity,p.direction,p.cost_cents,COALESCE(p.winner,r.winner) FROM paper_positions p LEFT JOIN research_calls r ON r.id=p.call_id WHERE p.status='OPEN' AND COALESCE(p.winner,r.winner) IS NOT NULL")
            for ident,quantity,direction,cost,winner in cur.fetchall():
                payout=quantity*100 if direction==winner else 0
                run(cur,postgres,"UPDATE paper_positions SET status='SETTLED',winner=?,payout_cents=?,pnl_cents=?,settled_at=?,mark_cents=?,mark_epoch=? WHERE id=? AND status='OPEN'",(winner,payout,payout-cost,stamp(),payout,time.time(),ident))
                if cur.rowcount:
                    learning.finalize_position(cur,postgres,run,ident,payout,'SETTLEMENT_FALLBACK')
                    run(cur,postgres,'UPDATE paper_account SET cash_cents=cash_cents+? WHERE id=\'main\'',(payout,))
                    run(cur,postgres,"UPDATE paper_orders SET status='EXPIRED_UNFILLED',updated_at=?,detail=? WHERE position_id=? AND status NOT IN ('FILLED','CANCELLED')",(stamp(),'Could not sell before expiry; official settlement applied as fallback',ident))
        finally:cur.close()

def full_exit(book,direction,quantity,slippage):
    net,missing=exit_mark(book,direction,quantity,slippage)
    if missing:return None
    levels=book['bids'] if direction=='UP' else sorted([(round(1-p,8),q) for p,q in book['offers']],reverse=True)
    lots=[];remaining=quantity
    for px,available in levels:
        q=min(remaining,available)
        if q:lots.append((max(0,float(Decimal(str(px))-Decimal(str(slippage)))),q));remaining-=q
        if not remaining:break
    _,fee=cost_lots(lots)
    return {'net_cents':net,'fee_cents':fee,'lots':lots,'quantity':quantity}

def opportunity_context(quote):
    examples={}
    for direction,key in [('UP','up_entry'),('DOWN','down_entry')]:
        price=number((quote or {}).get(key))
        if price is None or not RISK['min_price']<=price+RISK['slippage']<=RISK['max_price']:continue
        q=max(1,int((RISK['max_trade_cents']/100)/(price+RISK['slippage']+.02)))
        cost,_=cost_lots([(price+RISK['slippage'],q)])
        low=price;high=.999
        for _ in range(24):
            bid=(low+high)/2
            fill=full_exit({'bids':[(bid,q)],'offers':[]},'UP',q,RISK['slippage'])
            if fill and fill['net_cents']>=cost*(1+RISK['take_profit_pct']):high=bid
            else:low=bid
        fill=full_exit({'bids':[(high,q)],'offers':[]},'UP',q,RISK['slippage'])
        examples[direction]={'current_entry_ask':price,'example_quantity':q,'example_cost_cents':cost,
                             'minimum_exit_bid_for_net_target':round(high,4) if fill and fill['net_cents']>=cost*(1+RISK['take_profit_pct']) else None}
    return {'mode':'PAPER_ONLY','max_trade_usd':RISK['max_trade_cents']/100,'net_profit_target_pct':100*RISK['take_profit_pct'],
            'max_hold_seconds':max(0,min(180,int(market_start((quote or {}).get('market'))+900-time.time()-RISK['exit_before_expiry_seconds']))) if market_start((quote or {}).get('market')) is not None else 0,'entry_examples':examples,
            'note':'Fee-aware scenarios, not predicted prices or calibrated probabilities. Current executable depth is checked separately.'}

def position_context(execute,market):
    rows=execute("SELECT id,direction,quantity,cost_cents,mark_cents,mark_epoch,created_at FROM paper_positions WHERE market=? AND status='OPEN'",(market,),fetch=True)
    if not rows:return None
    ident,direction,q,cost,mark,epoch,created=rows[0]
    return {'position_id':ident,'direction':direction,'quantity':q,'entry_cost_cents':cost,
            'last_exit_mark_cents':mark,'net_liquidation_return_pct':round(100*(mark-cost)/cost,2),
            'mark_age_seconds':round(time.time()-epoch,1) if epoch else None,'opened_at':created}

def favorable_trend_strength(signal,direction):
    """Conservative evidence score, not a calibrated probability.
    Only recent, independently observed market data can extend a paper winner."""
    if not signal or signal.get('status')!='ready':return 0
    age=time.time()-float(signal.get('captured_epoch') or 0)
    if age<0 or age>60 or signal.get('verdict')!=direction:return 0
    snapshot=signal.get('snapshot') or {}
    if (snapshot.get('reference_price') or {}).get('status')!='fresh':return 0
    chart=snapshot.get('chart_pattern') or {}
    pattern=str(chart.get('pattern') or chart.get('trend') or chart.get('label') or '').upper()
    if 'DOWNTREND' in pattern or 'DOWN' in pattern:chart_direction='DOWN'
    elif 'UPTREND' in pattern or 'UP' in pattern:chart_direction='UP'
    else:chart_direction=None
    if chart_direction!=direction:return 0
    score=1
    if signal.get('confidence')=='HIGH':score+=1
    if signal.get('confidence')=='MEDIUM':score+=1
    return score


async def manage_exits(client,execute,connect,postgres,live_signal=None):
    # Keep the minimum 8% net target on existing paper positions.
    execute("UPDATE paper_orders SET threshold=? WHERE kind='TAKE_PROFIT' AND position_id IN (SELECT id FROM paper_positions WHERE status='OPEN') AND threshold<?",
            (RISK['take_profit_pct'],RISK['take_profit_pct']))
    rows=execute("SELECT id,market,direction,quantity,cost_cents FROM paper_positions WHERE status='OPEN'",fetch=True)
    for ident,market,direction,quantity,cost in rows:
        orders=execute('SELECT kind,threshold,status FROM paper_orders WHERE position_id=?',(ident,),fetch=True)
        terms={kind:(threshold,status) for kind,threshold,status in orders}
        forced='TIME_EXIT' in terms and time.time()>=terms['TIME_EXIT'][0]
        stop_working=terms.get('STOP_LOSS',(0,''))[1] in ('WORKING','UNFILLED')
        fill=None;book=None;problem=None
        try:
            book=await fetch_book(client,market);fill=full_exit(book,direction,quantity,RISK['slippage'])
            if fill is None:problem='Insufficient exit depth for a full-position simulated sell'
        except Exception as exc:problem='Exit book unavailable: '+type(exc).__name__
        if fill is not None:learning.observe_position_peak(execute,ident,market,cost,fill['net_cents'])
        roi=(fill['net_cents']-cost)/cost if fill and cost else None
        if roi is not None:_profit_peaks[ident]=max(_profit_peaks.get(ident,roi),roi)
        peak=_profit_peaks.get(ident,0)
        # Aim for 15% only while an 8%-plus winning position continues improving.
        # Protect profits by selling after a 2.5 percentage-point retracement.
        # All comparisons use full-exit proceeds net of modeled fees and slippage.
        live_signal=live_signal or {}
        trend_score=favorable_trend_strength(live_signal,direction)
        # At 8% net, only stretch to 15% with a fresh matching chart trend
        # and AI directional forecast. Otherwise take the observed profit.
        stretch_allowed=trend_score>=2
        stretch_hit=roi is not None and roi>=RISK['stretch_profit_pct']
        floor_exit=roi is not None and roi>=RISK['take_profit_pct'] and not stretch_allowed
        trailing_exit=(roi is not None and peak>=RISK['take_profit_pct'] and
                       roi<=peak-RISK['profit_trail_pct'])
        short_time_exit=(roi is not None and roi>=RISK['take_profit_pct'] and
                         time.time()>=market_start(market)+900-RISK['exit_before_expiry_seconds']-30)

        position=(live_signal.get('snapshot') or {}).get('paper_position') or {}
        opp=live_signal.get('profit_opportunity') or {};fresh=live_signal.get('status')=='ready' and 0<=time.time()-live_signal.get('captured_epoch',0)<=60 and position.get('position_id')==ident
        ai_profit=fresh and opp.get('position_action')=='TAKE_PROFIT' and roi is not None and roi>0
        ai_risk=fresh and opp.get('position_action')=='EXIT_RISK'
        kind='TIME_EXIT' if forced else 'STOP_LOSS' if ai_risk or stop_working or (roi is not None and roi<=-terms.get('STOP_LOSS',(RISK['stop_loss_pct'],''))[0]) else 'TAKE_PROFIT' if ai_profit or floor_exit or stretch_hit or trailing_exit or short_time_exit else None
        if kind is None:continue
        with connect() as c:
            cur,account=locked_account(c,postgres)
            try:
                run(cur,postgres,"SELECT status FROM paper_positions WHERE id=?",(ident,))
                if cur.fetchone()[0]!='OPEN':continue
                if book is not None and time.time()-book['book_epoch']>30:fill=None;problem='Exit book became stale'
                if fill is None:
                    run(cur,postgres,"UPDATE paper_orders SET status='UNFILLED',updated_at=?,detail=? WHERE position_id=? AND kind=?",(stamp(),problem or 'No executable depth',ident,kind));continue
                _profit_peaks.pop(ident,None)
                run(cur,postgres,"UPDATE paper_positions SET status='CLOSED',closed_at=?,exit_reason=?,exit_net_cents=?,exit_fee_cents=?,pnl_cents=?,mark_cents=?,mark_epoch=? WHERE id=? AND status='OPEN'",(stamp(),kind,fill['net_cents'],fill['fee_cents'],fill['net_cents']-cost,fill['net_cents'],time.time(),ident))
                if not cur.rowcount:continue
                learning.finalize_position(cur,postgres,run,ident,fill['net_cents'],kind)
                run(cur,postgres,"UPDATE paper_account SET cash_cents=cash_cents+? WHERE id='main'",(fill['net_cents'],))
                run(cur,postgres,"UPDATE paper_orders SET status='FILLED',updated_at=?,detail=? WHERE position_id=? AND kind=?",(stamp(),json.dumps({'simulated_exit':fill,'book_epoch':book['book_epoch']}),ident,kind))
                run(cur,postgres,"UPDATE paper_orders SET status='CANCELLED',updated_at=?,detail='Other protective exit filled' WHERE position_id=? AND kind<>? AND status<>'FILLED'",(stamp(),ident,kind))
            finally:cur.close()

async def update_marks(client,execute,connect,postgres):
    rows=execute("SELECT id,market,direction,quantity FROM paper_positions WHERE status='OPEN'",fetch=True)
    async def one(row):
        ident,market,direction,qty=row
        try:
            book=await fetch_book(client,market);value,unfilled=exit_mark(book,direction,qty,RISK['slippage']);return ident,value,time.time(),unfilled
        except Exception:return ident,0,time.time(),qty
    marks=await asyncio.gather(*(one(row) for row in rows))
    with connect() as c:
        cur,account=locked_account(c,postgres)
        try:
            for ident,value,epoch,unfilled in marks:run(cur,postgres,"UPDATE paper_positions SET mark_cents=?,mark_epoch=? WHERE id=? AND status='OPEN'",(value,epoch,ident))
            run(cur,postgres,"SELECT COALESCE(SUM(mark_cents),0) FROM paper_positions WHERE status='OPEN'")
            total_mark=int(cur.fetchone()[0]);cash=account[1];equity=cash+total_mark;high=max(account[5],equity);draw=max(account[6],high-equity)
            run(cur,postgres,'UPDATE paper_account SET high_water_cents=?,max_drawdown_cents=? WHERE id=\'main\'',(high,draw))
            run(cur,postgres,'INSERT INTO paper_equity (id,created_at,equity_cents,cash_cents,open_mark_cents) VALUES (?,?,?,?,?)',(str(uuid.uuid4()),stamp(),equity,cash,total_mark))
        finally:cur.close()

async def cycle(client,execute,connect,postgres,live_signal=None):
    global _last_mark, _last_entry_scan, _last_settlement_scan
    now_mono=time.monotonic()
    # Maintain fast protective exits without hammering PostgreSQL on every tick.
    await manage_exits(client,execute,connect,postgres,live_signal)
    if now_mono-_last_settlement_scan>=15:
        settle_positions(connect,postgres)
        learning.expire_unobserved(execute)
        _last_settlement_scan=now_mono
    if now_mono-_last_entry_scan<5:
        if now_mono-_last_mark>=30:
            await update_marks(client,execute,connect,postgres)
            _last_mark=time.monotonic()
        return
    _last_entry_scan=now_mono
    signal=live_signal or {};key=signal.get('paper_signal_id')
    if signal.get('status')=='ready' and key and 0<=time.time()-signal.get('captured_epoch',0)<=60:
        market=signal.get('market');opp=signal.get('profit_opportunity') or {}
        snapshot=signal.get('snapshot') or {};created=datetime.fromtimestamp(signal['captured_epoch'],timezone.utc).isoformat()
        call={'direction':'UP' if opp.get('action')=='ENTER_UP' else 'DOWN' if opp.get('action')=='ENTER_DOWN' else 'WAIT',
              'source':'AI_REVIEW','source_mode':'LIVE','profit_action':opp.get('action'),
              'confidence':opp.get('confidence'),'reason':opp.get('reason'),
              'max_entry_price':opp.get('max_entry_price'),'target_exit_bid':opp.get('target_exit_bid')}
        execute('INSERT INTO paper_live_calls (id,market,created_at,snapshot_json,calls_json) VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING',(key,market,created,json.dumps(snapshot),json.dumps({'AI':call})))
    started=execute("SELECT started_at FROM paper_account WHERE id='main'",fetch=True)[0][0]
    dip=polymarket_dip_signal(execute,signal)
    if dip:execute('INSERT INTO paper_live_calls (id,market,created_at,snapshot_json,calls_json) VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING',dip)
    cutoff=max(started,datetime.fromtimestamp(time.time()-60,timezone.utc).isoformat())
    rows=execute('SELECT market,id,created_at,snapshot_json,calls_json FROM paper_live_calls WHERE market=? AND created_at>=? ORDER BY created_at DESC LIMIT 3',
                 (signal.get('market') or '',cutoff),fetch=True)
    for row in rows:
        await try_entry(client,execute,connect,postgres,row)
        execute('UPDATE paper_live_calls SET processed=1 WHERE id=?',(row[1],))
    if time.monotonic()-_last_mark>=30:
        await update_marks(client,execute,connect,postgres);_last_mark=time.monotonic()

def missed_profit_learning(execute):
    """Retrospective, fee-aware analysis of skipped paper entries; never self-modifies live trading rules."""
    attempts=execute("SELECT market,created_at,reason,detail_json FROM paper_attempts WHERE status='SKIPPED' ORDER BY created_at DESC LIMIT 200",fetch=True)
    outcomes=[]
    for market,at,reason,raw in attempts:
        # No historical executable order book is persisted for skipped trades yet.
        # Therefore these are learning candidates, not proven missed profits.
        calls=execute("SELECT calls_json,snapshot_json FROM paper_live_calls WHERE market=? ORDER BY created_at DESC LIMIT 1",(market,),fetch=True)
        if not calls:continue
        try:
            call=json.loads(calls[0][0]).get('AI') or {}
            snapshot=json.loads(calls[0][1])
        except (TypeError,ValueError):continue
        direction=call.get('direction')
        if direction not in ('UP','DOWN'):continue
        resolved=execute("SELECT winner FROM research_calls WHERE market=? AND winner IS NOT NULL LIMIT 1",(market,),fetch=True)
        outcome=resolved[0][0] if resolved else None
        outcomes.append({'market':market,'at':at,'direction':direction,'reason':reason,
                         'confidence':call.get('confidence'),'pattern':(snapshot.get('chart_pattern') or {}).get('pattern'),
                         'outcome':outcome,'direction_won':direction==outcome if outcome in ('UP','DOWN') else None,
                         'classification':'UNVERIFIED_MISSED_PROFIT',
                         'note':'Winning direction does not establish a profitable, executable entry and exit.'})
    by_reason={}
    for item in outcomes:
        if item['direction_won'] is None:continue
        group=by_reason.setdefault(item['reason'],{'resolved':0,'direction_wins':0})
        group['resolved']+=1
        group['direction_wins']+=int(item['direction_won'])
    for group in by_reason.values():
        group['direction_win_rate_pct']=round(100*group['direction_wins']/group['resolved'],1)
    return {'candidates':outcomes[:50],'patterns_by_skip_reason':by_reason,
            'verified_missed_profit_count':0,
            'status':'OBSERVATION_ONLY',
            'note':'Learning observations are retrospective and not executable-profit proof. Trading parameters remain unchanged until historical order-book capture and out-of-sample validation exist.'}

def control(connect,postgres,action):
    if action not in ('pause','resume','stop'):raise ValueError('Unsupported paper action')
    with connect() as c:
        cur,_=locked_account(c,postgres)
        try:
            run(cur,postgres,"UPDATE paper_account SET paused=? WHERE id='main'",(1 if action in ('pause','stop') else 0,))
            if action=='stop':run(cur,postgres,"UPDATE paper_orders SET threshold=?,status='WORKING',updated_at=?,detail='Emergency paper stop requested' WHERE kind='TIME_EXIT' AND position_id IN (SELECT id FROM paper_positions WHERE status='OPEN')",(time.time(),stamp()))
        finally:cur.close()

def report(execute):
    a=execute("SELECT initial_cents,cash_cents,paused,started_at,risk_json,high_water_cents,max_drawdown_cents FROM paper_account WHERE id='main'",fetch=True)[0]
    positions=execute('SELECT market,direction,quantity,cost_cents,fee_cents,status,created_at,settled_at,winner,payout_cents,pnl_cents,mark_cents,mark_epoch,exit_reason,exit_fee_cents,closed_at FROM paper_positions ORDER BY created_at DESC LIMIT 50',fetch=True)
    run_rows=execute("SELECT status,COUNT(*),COALESCE(SUM(pnl_cents),0),COALESCE(SUM(fee_cents+COALESCE(exit_fee_cents,0)),0) FROM paper_positions GROUP BY status",fetch=True)
    open_cost=execute("SELECT COALESCE(SUM(cost_cents),0),COALESCE(SUM(mark_cents),0) FROM paper_positions WHERE status='OPEN'",fetch=True)[0]
    totals={status:{'count':n,'pnl_cents':pnl,'fees_cents':fees} for status,n,pnl,fees in run_rows}
    winloss=execute("SELECT SUM(CASE WHEN pnl_cents>0 THEN 1 ELSE 0 END),SUM(CASE WHEN pnl_cents<=0 THEN 1 ELSE 0 END),COALESCE(SUM(CASE WHEN pnl_cents>0 THEN pnl_cents ELSE 0 END),0),COALESCE(SUM(CASE WHEN pnl_cents<0 THEN -pnl_cents ELSE 0 END),0) FROM paper_positions WHERE status IN ('CLOSED','SETTLED')",fetch=True)[0]
    wins,losses,gain,loss=winloss;wins=wins or 0;losses=losses or 0;n=wins+losses
    events=execute('SELECT market,created_at,status,reason,detail_json FROM paper_attempts ORDER BY created_at DESC LIMIT 25',fetch=True)
    shadow_counts=execute("SELECT status,COUNT(*) FROM paper_missed_opportunities GROUP BY status",fetch=True)
    shadow_recent=execute("""SELECT market,direction,observed_epoch,entry_ask,quantity,entry_cost_cents,
           status,best_exit_bid,best_exit_epoch,best_net_cents,max_roi_pct,resolved_epoch
           FROM paper_missed_opportunities WHERE status<>'WATCHING' ORDER BY COALESCE(resolved_epoch,observed_epoch) DESC LIMIT 40""",fetch=True)
    shadow_counts=dict(shadow_counts)
    learning_report=learning.report(execute)
    blocker_rows=execute("SELECT reason,COUNT(*) FROM paper_attempts WHERE status='SKIPPED' GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 12",fetch=True)
    signal_rows=execute("SELECT COUNT(*),SUM(CASE WHEN processed=0 THEN 1 ELSE 0 END) FROM paper_live_calls",fetch=True)[0]
    latest_signal=execute('SELECT created_at,calls_json FROM paper_live_calls ORDER BY created_at DESC LIMIT 1',fetch=True)
    last_signal_age=round(time.time()-datetime.fromisoformat(latest_signal[0][0]).timestamp(),1) if latest_signal else None
    diagnostic={'signals_received':signal_rows[0] or 0,'signals_pending':signal_rows[1] or 0,'last_signal_age_seconds':last_signal_age,'skipped_by_reason':[{'reason':reason,'count':count} for reason,count in blocker_rows],'guidance':'No signal: inspect /api/status ai_confirmation and market data. Skipped: inspect attempts and skipped_by_reason. Filled: inspect positions and protective orders.'}
    curve=execute('SELECT created_at,equity_cents FROM paper_equity ORDER BY created_at DESC LIMIT 240',fetch=True)
    days=day_bounds();daily_loss=execute("SELECT COALESCE(SUM(CASE WHEN pnl_cents<0 THEN -pnl_cents ELSE 0 END),0) FROM paper_positions WHERE status IN ('CLOSED','SETTLED') AND COALESCE(closed_at,settled_at)>=? AND COALESCE(closed_at,settled_at)<?",days,fetch=True)[0][0]
    enabled=os.getenv('PAPER_TRADING_ENABLED','true').lower() in ('true','1','yes')
    return {'mode':'PAPER_ONLY','real_orders_enabled':False,'status':'paused' if a[2] or not enabled else 'running',
            'initial_cents':a[0],'cash_cents':a[1],'open_cost_cents':open_cost[0],'open_liquidation_mark_cents':open_cost[1],
            'conservative_equity_cents':a[1]+open_cost[1],'net_liquidation_pnl_cents':a[1]+open_cost[1]-a[0],
            'realized_pnl_cents':a[1]+open_cost[0]-a[0],'total_fees_cents':sum(x['fees_cents'] for x in totals.values()),
            'entry_fees_cents':execute('SELECT COALESCE(SUM(fee_cents),0) FROM paper_positions',fetch=True)[0][0],
            'max_drawdown_cents':a[6],'closed_trades':n,'settled_trades':n,'winning_trades':wins,'losing_trades':losses,
            'win_rate_pct':round(100*wins/n,1) if n else None,'profit_factor':round(gain/loss,3) if loss else None,
            'daily_realized_losses_cents':daily_loss,'daily_loss_risk_used_cents':daily_loss+open_cost[0],
            'started_at':a[3],'risk':risk_settings(json.loads(a[4])),'sample_label':'INSUFFICIENT SAMPLE' if n<100 else 'OBSERVED PAPER SAMPLE',
            'positions':[dict(zip(('market','direction','quantity','cost_cents','fee_cents','status','created_at','settled_at','winner','payout_cents','pnl_cents','mark_cents','mark_epoch','exit_reason','exit_fee_cents','closed_at'),x)) for x in positions],
            'attempts':[{'market':m,'created_at':t,'status':s,'reason':why,'details':json.loads(detail)} for m,t,s,why,detail in events],
            'trading_diagnostics':diagnostic,
             'retrospective_missed_profit_analysis':missed_profit_learning(execute),
            'missed_profit_learning':learning_report,
            'missed_opportunity_counts':shadow_counts,
            'missed_opportunities':[dict(zip(('market','direction','observed_epoch','entry_ask','quantity','entry_cost_cents',
                'status','best_exit_bid','best_exit_epoch','best_net_cents','max_roi_pct','resolved_epoch'),row)) for row in shadow_recent],
            'missed_opportunity_note':'Forward observed Polymarket BBO top-of-book hypothetical trades (10 contracts, taker-fee and adverse slippage model), sampled approximately every 20s per direction. MISSED_QUOTED_PROFIT means a later quote hypothetically beat the net 8% target within 180s without a matching paper position. Not a guaranteed fill or realized profit; depth, latency and slippage can differ.',
            'entry_mode':'LIVE_ANYTIME',
            'latest_live_signal':[{'market':m,'captured_at':t,'call':json.loads(c).get('AI')} for m,t,c in execute('SELECT market,created_at,calls_json FROM paper_live_calls ORDER BY created_at DESC LIMIT 1',fetch=True)],
            'orders':[dict(zip(('market','kind','status','threshold','updated_at','detail'),x)) for x in execute('SELECT market,kind,status,threshold,updated_at,detail FROM paper_orders ORDER BY updated_at DESC LIMIT 60',fetch=True)],
            'unfilled_exit_orders':execute("SELECT COUNT(*) FROM paper_orders WHERE status='UNFILLED'",fetch=True)[0][0],
            'equity_history':[{'at':t,'equity_cents':v} for t,v in reversed(curve)],
            'note':'Fake money only. Fresh LOW/MEDIUM/HIGH live AI profit opportunities (LOW entries capped at $5) at any point in the round; one filled entry per market; quoted odds and all-in entry cost strictly below 85 percent; fresh two-sided entry book required. Simulated take profit +8% / stop loss -8% net of fees, max hold 180 seconds, and exit at least 180 seconds before expiry. Full-position sell fills only; unfilled exits are flagged and retried, not fabricated. Missing exit depth is valued at zero until official settlement. Paper fills, timing and profitability can differ from real trading. Pause stops entries, not protective sells or settlement fallback. Holding to settlement is an exceptional unfilled-exit fallback, not the strategy. Daily loss budget uses Chicago time, gross realized losses plus all open cost. Sandbox controls are shared with dashboard visitors.'}

