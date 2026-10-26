"""Forward-only checkpoints, reference-price evidence and paper entry costs."""
import json, math, time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN

VERSION='forward-15m-v1'
CHECKPOINTS=[('EARLY',720),('HALFWAY',450),('LATE',180)]
FEE_SOURCE='https://docs.polymarket.us/fees'
FEE_THETA=0.0695  # Standard US taker schedule effective 2026-10-19 13:00 ET.

def checkpoint_plan(seconds_left,captured):
    return {"due":[name for name,trigger in CHECKPOINTS if trigger-60<=seconds_left<=trigger and name not in captured],
            "missed":[name for name,trigger in CHECKPOINTS if seconds_left<trigger-60 and name not in captured]}

def number(v):
    if isinstance(v,dict):v=v.get('value')
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):return None

def market_start(slug):
    try:return datetime.strptime(str(slug).split('btc-updown-15m-',1)[1],'%Y-%m-%d-%H%Mz').replace(tzinfo=timezone.utc).timestamp()
    except (ValueError,IndexError):return None

async def reference_prices(client,market):
    """Public Polymarket US reference feed. Never treat gap-filled buckets as ticks."""
    now=time.time();start=market_start(market.get('slug'))
    if start is None:return {'status':'unavailable','reason':'Invalid market time','points':[]}
    if (market.get('assetPriceTerms') or {}).get('indexSymbol')!='BRTI':
        return {'status':'unavailable','reason':'Contract does not explicitly identify BRTI','points':[]}
    try:
        r=await client.get('https://gateway.polymarket.us/v1/asset-prices/history',params={
            'assets':'crypto:btc','from':int(start)-60,'to':0,'bucket':'ASSET_PRICE_BUCKET_1S'},timeout=6)
        r.raise_for_status();series=r.json().get('series',[])
        s=next(x for x in series if x.get('asset',{}).get('symbol')=='btc' and x.get('asset',{}).get('assetClass')=='ASSET_CLASS_CRYPTO' and x.get('currency')=='USD')
        points={}
        for x in s.get('candles',[]):
            t=number(x.get('timestamp'));p=number(x.get('close'))
            if t is not None and p is not None and p>0 and int(x.get('tickCount',0))>0 and start-60<=t<=now:
                points[int(t)]=p
        ordered=sorted(points.items())
        if not ordered:raise ValueError('No published ticks')
        age=max(0,now-ordered[-1][0]);target_points=[points[t] for t in range(int(start)-60,int(start)) if t in points]
        final_start=start+840
        final_points=[points[t] for t in range(int(final_start),int(start)+900) if t<=now and t in points]
        return {'status':'fresh' if age<=15 else 'stale','source':'Polymarket US BTC reference / BRTI',
                'source_url':'https://docs.polymarket.us/api-reference/asset-prices/overview',
                'price':ordered[-1][1],'price_epoch':ordered[-1][0],'age_seconds':round(age,1),
                'points':ordered,'window_start':start,'window_end':start+900,
                'opening_average':round(sum(target_points)/60,2) if len(target_points)==60 else None,
                'opening_average_samples':len(target_points),
                'final_minute_average_so_far':round(sum(final_points)/len(final_points),2) if final_points else None,
                'final_minute_samples':len(final_points),
                'settlement_rule':'Official metadata/outcome remain authoritative. BTC uses the mean of 60 one-second reference prices immediately before each boundary; current final-minute mean is partial until all 60 seconds exist.'}
    except Exception as exc:return {'status':'unavailable','reason':type(exc).__name__,'points':[]}

def target_behavior(external,target,slug,reference=None,now=None):
    now=time.time() if now is None else now;target=number(target);start=market_start(slug)
    base={'status':'unavailable','target':target,'source':None,'observed_crossings':None}
    if not target or target<=0 or start is None:return base
    reference=reference or {}
    if reference.get('status')=='fresh':
        samples=[(float(t),float(p)) for t,p in reference.get('points',[]) if start<=t<=now and t<start+900];cadence=1;source=reference.get('source')
    else:
        samples=[(float(b[0])+60,float(b[4])) for b in (external.get('graph_evidence') or {}).get('candles_ohlcv',[]) if len(b)>=5 and start<=float(b[0]) and float(b[0])+60<=now and float(b[0])+60<=start+900];cadence=60;source=external.get('source','spot proxy')
    samples=sorted(dict(samples).items())
    if not samples:return base
    price=samples[-1][1];gap=100*(price/target-1)
    crossings=0;above=below=covered=0
    for (t0,p0),(t1,p1) in zip(samples,samples[1:]):
        if t1-t0!=cadence:continue
        covered+=cadence
        if p0>target:above+=cadence
        if p0<target:below+=cadence
        if (p0-target)*(p1-target)<0:crossings+=1
    run=0;sign=1 if price>target else -1 if price<target else 0
    for (t0,p0),(t1,p1) in reversed(list(zip(samples,samples[1:]))):
        if t1-t0!=cadence or sign==0 or (p0-target)*sign<=0 or (p1-target)*sign<=0:break
        run+=cadence
    prior=next((p for t,p in reversed(samples) if t<=samples[-1][0]-180),None)
    old_gap=100*(prior/target-1) if prior else None
    growth=abs(gap)-abs(old_gap) if old_gap is not None else None
    return {'status':'available','source':source,'is_reference':cadence==1,'target':target,'price':price,
            'price_age_seconds':round(max(0,now-samples[-1][0]),1),'target_gap_pct':round(gap,5),
            'side':'UP' if gap>0 else 'DOWN' if gap<0 else 'TIED',
            'observed_crossings':crossings,'seconds_above':above,'seconds_below':below,
            'observed_seconds':covered,'elapsed_seconds':round(max(0,min(900,now-start))),
            'coverage_pct':round(100*covered/max(1,min(900,now-start)),1),
            'current_side_run_seconds':run,'absolute_gap_change_3m_pct':round(growth,5) if growth is not None else None,
            'lead_behavior':'GROWING' if growth is not None and growth>.005 else 'SHRINKING' if growth is not None and growth<-.005 else 'STEADY' if growth is not None else 'UNKNOWN',
            'near_target':abs(gap)<.025,'note':'Observed contiguous intervals only; crossings and duration exclude gaps. Reference and spot proxy are labeled separately.'}

async def market_quote(client,slug):
    try:
        r=await client.get('https://gateway.polymarket.us/v1/markets/'+slug+'/bbo',timeout=5);r.raise_for_status()
        d=r.json().get('marketData',{});bid=number(d.get('bestBid'));ask=number(d.get('bestAsk'))
        if d.get('marketSlug')!=slug or d.get('state')!='MARKET_STATE_OPEN':raise ValueError('Market not open or wrong quote')
        if bid is not None and not 0<=bid<=1:raise ValueError('Invalid bid')
        if ask is not None and not 0<=ask<=1:raise ValueError('Invalid ask')
        if (bid is None and ask is None) or (bid is not None and ask is not None and bid>ask):raise ValueError('Empty or crossed book')
        return {'status':'available','market':slug,'bid':bid,'ask':ask,'up_entry':ask,'down_entry':round(1-bid,6) if bid is not None else None,
                'spread':round(ask-bid,6) if ask is not None and bid is not None else None,'fetched_epoch':time.time(),'source':'Polymarket US BBO',
                'note':'UP uses the YES ask; DOWN uses 1 minus YES bid. Indicative top of book, not a fill or depth guarantee.'}
    except Exception as exc:return {'status':'unavailable','market':slug,'reason':type(exc).__name__}

def entry_cost(price,contracts=100,slippage=0.005,theta=FEE_THETA):
    p=number(price);c=number(contracts);s=number(slippage);theta=number(theta)
    if p is None or c is None or s is None or theta is None or not 0<p<1 or c<=0 or c>1e6 or not 0<=s<=.25 or not 0<=theta<=1:return {'status':'unavailable','reason':'Invalid price, quantity, slippage or fee coefficient'}
    dp=Decimal(str(p))+Decimal(str(s));fill=float(dp)
    if dp>=1:return {'status':'unavailable','reason':'Price plus slippage reaches payout ceiling'}
    dc=Decimal(str(c));fee=(Decimal(str(theta))*dc*dp*(1-dp)).quantize(Decimal('.01'),rounding=ROUND_HALF_EVEN)
    total=float(dp*dc+fee)
    return {'status':'estimated','contracts':c,'quoted_entry':p,'assumed_slippage_per_contract':s,'assumed_fill':round(fill,6),
            'fee_usd':float(fee),'total_cost_usd':round(total,4),'breakeven_win_rate_pct':round(100*total/c,3),
            'net_if_win_usd':round(c-total,4),'net_if_loss_usd':round(-total,4),
            'fee_theta':theta,'fee_schedule':'US standard taker, verified 2026-10-25; effective 2026-10-19 13:00 ET',
            'fee_source':FEE_SOURCE,'note':'Paper single-fill hold-to-settlement scenario, $1 payout. No rebates or actual executions; fill splitting, spread changes, size and fee updates can change costs.'}

def confluence_forecast(ai_call, target_behavior, reference_price, chart_indicators):
    """Forward-only, selective RESEARCH signal. Does not rewrite AI calls or trade.

    Requirements are fixed before settlement, and abstentions remain countable.
    This is a candidate filter, not a validated accuracy improvement.
    """
    def no(reason):
        return {"direction":"WAIT","source":"CONFLUENCE_ABSTAIN",
                "confidence":"UNRATED","reason":reason}
    if not isinstance(ai_call,dict) or ai_call.get("source")!="AI_REVIEW" or ai_call.get("direction") not in ("UP","DOWN"):
        return no("No verified live AI forecast")
    if ai_call.get("confidence") not in ("MEDIUM","HIGH"):
        return no("AI evidence confidence LOW or unavailable")
    behavior=target_behavior if isinstance(target_behavior,dict) else {}
    reference=reference_price if isinstance(reference_price,dict) else {}
    chart=chart_indicators if isinstance(chart_indicators,dict) else {}
    if reference.get("status")!="fresh" or behavior.get("status")!="available" or behavior.get("is_reference") is not True:
        return no("No fresh official-index target evidence")
    age=number(behavior.get("price_age_seconds"))
    coverage=number(behavior.get("coverage_pct"))
    gap=number(behavior.get("target_gap_pct"))
    atr=number(chart.get("atr14_pct"))
    duration=number(behavior.get("current_side_run_seconds"))
    crossings=number(behavior.get("observed_crossings"))
    if age is None or age>15 or coverage is None or coverage<45:
        return no("Index observation too old or sparse")
    if gap is None or atr is None or abs(gap)<max(.05,.40*atr):
        return no("Target lead too small relative to volatility")
    if behavior.get("near_target") is True or duration is None or duration<45:
        return no("Near target or insufficient sustained lead")
    if crossings is None or crossings>=4 or behavior.get("lead_behavior")=="SHRINKING":
        return no("Repeated crossings or shrinking lead")
    side="UP" if gap>0 else "DOWN"
    if ai_call.get("direction")!=side or behavior.get("side")!=side:
        return no("AI forecast opposes the current official-index side")
    return {"direction":side,"source":"CONFLUENCE_FORWARD",
            "confidence":ai_call["confidence"],
            "reason":"Fresh index lead, sustained target side and AI agreement"}

def wilson(w,n):
    if not n:return None
    z=1.96;p=w/n;den=1+z*z/n;mid=(p+z*z/(2*n))/den;half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/den
    return [round(100*(mid-half),1),round(100*(mid+half),1)]

def summarize(rows):
    """Only actually captured calls; no manufactured predictions for historical rounds."""
    groups={};confidence={};paired={};recent=[]
    for market,checkpoint,created,snapshot_json,calls_json,winner in rows:
        try:snapshot=json.loads(snapshot_json);calls=json.loads(calls_json)
        except (TypeError,ValueError):continue
        for name,call in calls.items():
            key=(checkpoint,name);g=groups.setdefault(key,{'checkpoint':checkpoint,'strategy':name,'wins':0,'losses':0,'pending':0,'no_call':0,'paper_costed':0,'paper_pnl_usd':0.0})
            direct=call.get('direction');eligible=direct in ('UP','DOWN') and (name!='AI' or call.get('source')=='AI_REVIEW')
            if not eligible:g['no_call']+=1
            elif not winner:g['pending']+=1
            else:
                result='wins' if direct==winner else 'losses';g[result]+=1
                cost=call.get('entry_cost') or {}
                if cost.get('status')=='estimated':
                    g['paper_costed']+=1;g['paper_pnl_usd']+=cost['net_if_win_usd'] if direct==winner else cost['net_if_loss_usd']
                if name=='AI':
                    label=call.get('confidence','UNRATED');c=confidence.setdefault((checkpoint,label),{'checkpoint':checkpoint,'confidence':label,'wins':0,'losses':0})
                    c[result]+=1
            if name=='AI' and winner and eligible:
                for baseline in ('INDICATORS','INDICATOR_BINARY','TARGET_SIDE','PRICE_ACTION','CONFLUENCE','BOT_MACD','BOT_BOLLINGER','BOT_SUPERTREND','BOT_CONSENSUS'):
                    other=calls.get(baseline,{})
                    if other.get('direction') not in ('UP','DOWN'):continue
                    p=paired.setdefault((checkpoint,baseline),{'checkpoint':checkpoint,'baseline':baseline,'paired_rounds':0,'ai_wins':0,'baseline_wins':0,'ai_only_correct':0,'baseline_only_correct':0})
                    ai_ok=direct==winner;other_ok=other['direction']==winner
                    p['paired_rounds']+=1;p['ai_wins']+=int(ai_ok);p['baseline_wins']+=int(other_ok);p['ai_only_correct']+=int(ai_ok and not other_ok);p['baseline_only_correct']+=int(other_ok and not ai_ok)
        if len(recent)<30:recent.append({'market':market,'checkpoint':checkpoint,'captured_at':created,'winner':winner,'calls':calls,'target_behavior':snapshot.get('target_behavior'), 'news_assessment':snapshot.get('news_assessment'),'version':snapshot.get('version')})
    def finish(g):
        n=g['wins']+g['losses'];coverage=n+g.get('pending',0);total=coverage+g.get('no_call',0);g.update(graded=n,coverage_pct=round(100*coverage/total,1) if total else None,accuracy_pct=round(100*g['wins']/n,1) if n else None,interval_95_pct=wilson(g['wins'],n),sample_label='SMALL SAMPLE' if n<100 else 'OBSERVED SAMPLE')
        if 'paper_pnl_usd' in g:g['paper_pnl_usd']=round(g['paper_pnl_usd'],2)
        return g
    for p in paired.values():p['ai_advantage_percentage_points']=round(100*(p['ai_wins']-p['baseline_wins'])/p['paired_rounds'],1)
    return {'version':VERSION,'strategies':[finish(g) for g in groups.values()], 'confidence':[finish(c) for c in confidence.values()],
            'paired_comparisons':list(paired.values()),'recent':recent,
            'note':'Forward observations only. Independent public-indicator bot candidates are unvalidated shadow strategies; compare accuracy AND coverage at the same checkpoint, with no lookahead, AI copying, live orders, or retrospective outcome fitting. Confidence labels are uncalibrated; intervals are descriptive and do not adjust for dependence between rounds. Paper P&L is illustrative only.'}
