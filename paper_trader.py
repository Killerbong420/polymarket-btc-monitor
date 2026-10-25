"""Shared fake-money sandbox. GET-only exchange access; no real-order capability."""
import asyncio, json, math, os, time, uuid
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_EVEN
from zoneinfo import ZoneInfo
from research import number, market_start, FEE_THETA

INITIAL_CENTS=100000
RISK={'max_trade_cents':1000,'daily_loss_cents':2500,'max_open_cents':5000,
      'min_price':.10,'max_price':.90,'max_spread':.08,'slippage':.005,
      'take_profit_pct':.10,'stop_loss_pct':.08,'max_hold_seconds':180,'exit_before_expiry_seconds':180,
      'min_confidence':'MEDIUM','checkpoint':'HALFWAY','daily_timezone':'America/Chicago'}
_last_mark=0.0

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
        if fill>risk['max_price']:break
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
    run(cur,postgres,'INSERT INTO paper_attempts (market,created_at,status,reason,detail_json) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING',(market,stamp(),status,reason,json.dumps(detail)))

async def try_entry(client,execute,connect,postgres,row):
    market,call_id,created,snapshot_json,calls_json=row
    snapshot=json.loads(snapshot_json);call=(json.loads(calls_json).get('AI') or {})
    direction=call.get('direction');reason=None;book=None
    age=time.time()-datetime.fromisoformat(created).timestamp()
    if call.get('source')!='AI_REVIEW' or direction not in ('UP','DOWN'):reason='No actual AI directional review'
    elif call.get('confidence') not in ('MEDIUM','HIGH'):reason='AI confidence below paper-test minimum'
    elif not 0<=age<=60:reason='Saved call is too old for a new paper entry'
    elif (snapshot.get('reference_price') or {}).get('status')!='fresh':reason='No fresh reference-price evidence at decision time'
    elif number(snapshot.get('official_target')) is None:reason='Official target unavailable'
    elif market_start(market) is None or market_start(market)+900-time.time()<120:reason='Too close to contract expiry'
    if reason is None:
        try:book=await fetch_book(client,market)
        except Exception as exc:reason='Book unavailable: '+type(exc).__name__+': '+str(exc)[:90]
    with connect() as c:
        cur,account=locked_account(c,postgres)
        try:
            run(cur,postgres,'SELECT market FROM paper_attempts WHERE market=?',(market,))
            if cur.fetchone():return
            initial,cash,paused,started,risk_json,high,drawdown=account;risk={**RISK,**json.loads(risk_json)}
            if paused or os.getenv('PAPER_TRADING_ENABLED','true').lower() not in ('true','1','yes'):
                record_attempt(cur,postgres,market,'SKIPPED','Paper trading paused',{});return
            if created<started:return
            if reason is None and time.time()-datetime.fromisoformat(created).timestamp()>60:reason='Saved call became stale before simulated entry'
            if reason is None and (time.time()-book['book_epoch']>30 or market_start(market)+900-time.time()<120):reason='Book or contract became ineligible before entry'
            if reason:record_attempt(cur,postgres,market,'SKIPPED',reason,{'call_id':call_id});return
            run(cur,postgres,'SELECT COALESCE(SUM(cost_cents),0) FROM paper_positions WHERE status=\'OPEN\'')
            opened=int(cur.fetchone()[0]);daily=gross_daily_losses(cur,postgres)
            budget=min(cash,risk['max_trade_cents'],risk['max_open_cents']-opened,risk['daily_loss_cents']-daily-opened)
            if budget<=0:record_attempt(cur,postgres,market,'SKIPPED','Cash, open-risk or daily-loss budget reached',{'open_risk_cents':opened,'daily_realized_losses_cents':daily});return
            fill=entry_fill(book,direction,budget,risk)
            if not fill:record_attempt(cur,postgres,market,'SKIPPED','No eligible depth within entry price, spread and risk limits',{'budget_cents':budget,'spread':book.get('spread')});return
            evidence={'model_call':call,'decision_snapshot':snapshot,'entry':fill,'risk':risk,'book':book,'entry_delay_seconds':round(age,2)}
            position_id=str(uuid.uuid4())
            run(cur,postgres,'INSERT INTO paper_positions (id,market,call_id,direction,quantity,cost_cents,fee_cents,status,created_at,mark_cents,mark_epoch,evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (position_id,market,call_id,direction,fill['quantity'],fill['cost_cents'],fill['fee_cents'],'OPEN',stamp(),0,None,json.dumps(evidence)))
            for kind,threshold in [('TAKE_PROFIT',risk['take_profit_pct']),('STOP_LOSS',risk['stop_loss_pct']),('TIME_EXIT',min(time.time()+risk['max_hold_seconds'],market_start(market)+900-risk['exit_before_expiry_seconds']))]:
                run(cur,postgres,'INSERT INTO paper_orders (id,position_id,market,kind,status,threshold,created_at,updated_at,detail) VALUES (?,?,?,?,?,?,?,?,?)',
                    (str(uuid.uuid4()),position_id,market,kind,'WAITING',threshold,stamp(),stamp(),'Simulated protective sell; full fill required, no real order'))
            run(cur,postgres,'UPDATE paper_account SET cash_cents=cash_cents-? WHERE id=\'main\'',(fill['cost_cents'],))
            record_attempt(cur,postgres,market,'FILLED','Simulated depth fill',{'call_id':call_id,**fill})
        finally:cur.close()

def settle_positions(connect,postgres):
    with connect() as c:
        cur,account=locked_account(c,postgres)
        try:
            run(cur,postgres,"SELECT p.id,p.quantity,p.direction,p.cost_cents,r.winner FROM paper_positions p JOIN research_calls r ON r.id=p.call_id WHERE p.status='OPEN' AND r.winner IS NOT NULL")
            for ident,quantity,direction,cost,winner in cur.fetchall():
                payout=quantity*100 if direction==winner else 0
                run(cur,postgres,"UPDATE paper_positions SET status='SETTLED',winner=?,payout_cents=?,pnl_cents=?,settled_at=?,mark_cents=?,mark_epoch=? WHERE id=? AND status='OPEN'",(winner,payout,payout-cost,stamp(),payout,time.time(),ident))
                if cur.rowcount:
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

async def manage_exits(client,execute,connect,postgres):
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
        roi=(fill['net_cents']-cost)/cost if fill and cost else None
        kind='TIME_EXIT' if forced else 'STOP_LOSS' if stop_working or (roi is not None and roi<=-terms.get('STOP_LOSS',(RISK['stop_loss_pct'],''))[0]) else 'TAKE_PROFIT' if roi is not None and roi>=terms.get('TAKE_PROFIT',(RISK['take_profit_pct'],''))[0] else None
        if kind is None:continue
        with connect() as c:
            cur,account=locked_account(c,postgres)
            try:
                run(cur,postgres,"SELECT status FROM paper_positions WHERE id=?",(ident,))
                if cur.fetchone()[0]!='OPEN':continue
                if book is not None and time.time()-book['book_epoch']>30:fill=None;problem='Exit book became stale'
                if fill is None:
                    run(cur,postgres,"UPDATE paper_orders SET status='UNFILLED',updated_at=?,detail=? WHERE position_id=? AND kind=?",(stamp(),problem or 'No executable depth',ident,kind));continue
                run(cur,postgres,"UPDATE paper_positions SET status='CLOSED',closed_at=?,exit_reason=?,exit_net_cents=?,exit_fee_cents=?,pnl_cents=?,mark_cents=?,mark_epoch=? WHERE id=? AND status='OPEN'",(stamp(),kind,fill['net_cents'],fill['fee_cents'],fill['net_cents']-cost,fill['net_cents'],time.time(),ident))
                if not cur.rowcount:continue
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

async def cycle(client,execute,connect,postgres):
    global _last_mark
    await manage_exits(client,execute,connect,postgres)
    settle_positions(connect,postgres)
    started=execute("SELECT started_at FROM paper_account WHERE id='main'",fetch=True)[0][0]
    rows=execute("SELECT market,id,created_at,snapshot_json,calls_json FROM research_calls WHERE checkpoint='HALFWAY' AND created_at>=? AND winner IS NULL AND market NOT IN (SELECT market FROM paper_attempts) ORDER BY created_at DESC LIMIT 5",(started,),fetch=True)
    for row in rows:await try_entry(client,execute,connect,postgres,row)
    if time.monotonic()-_last_mark>=30:
        await update_marks(client,execute,connect,postgres);_last_mark=time.monotonic()

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
            'started_at':a[3],'risk':{**RISK,**json.loads(a[4])},'sample_label':'INSUFFICIENT SAMPLE' if n<100 else 'OBSERVED PAPER SAMPLE',
            'positions':[dict(zip(('market','direction','quantity','cost_cents','fee_cents','status','created_at','settled_at','winner','payout_cents','pnl_cents','mark_cents','mark_epoch','exit_reason','exit_fee_cents','closed_at'),x)) for x in positions],
            'attempts':[{'market':m,'created_at':t,'status':s,'reason':why,'details':json.loads(detail)} for m,t,s,why,detail in events],
            'orders':[dict(zip(('market','kind','status','threshold','updated_at','detail'),x)) for x in execute('SELECT market,kind,status,threshold,updated_at,detail FROM paper_orders ORDER BY updated_at DESC LIMIT 60',fetch=True)],
            'unfilled_exit_orders':execute("SELECT COUNT(*) FROM paper_orders WHERE status='UNFILLED'",fetch=True)[0][0],
            'equity_history':[{'at':t,'equity_cents':v} for t,v in reversed(curve)],
            'note':'Fake money only. One actual MEDIUM/HIGH AI halfway call per market; fresh two-sided entry book required. Simulated take profit +10% / stop loss -8% net of fees, max hold 180 seconds, and exit at least 180 seconds before expiry. Full-position sell fills only; unfilled exits are flagged and retried, not fabricated. Missing exit depth is valued at zero until official settlement. Paper fills, timing and profitability can differ from real trading. Pause stops entries, not protective sells or settlement fallback. Holding to settlement is an exceptional unfilled-exit fallback, not the strategy. Daily loss budget uses Chicago time, gross realized losses plus all open cost. Sandbox controls are shared with dashboard visitors.'}
