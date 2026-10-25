"""Collect 672 Polymarket US BTC 15-minute rounds. Never infer missing targets or winners.
Run: python historical_15m.py
Outputs: historical_15m_7d.json and historical_15m_7d.csv
"""
import asyncio,csv,json,re,math
from datetime import datetime,timedelta,timezone
from pathlib import Path
import httpx

BASE="https://gateway.polymarket.us"
END=datetime.now(timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)
START=END-timedelta(days=7)
def extract_target(obj):
 if not isinstance(obj,dict): return None
 terms=obj.get("assetPriceTerms")
 if isinstance(terms,dict):
  target=extract_target(terms)
  if target is not None: return target
 for key in ("priceToBeat","price_to_beat","targetPrice","target_price","strikePrice","strike_price"):
  v=obj.get(key)
  if isinstance(v,dict): v=v.get("value")
  try:
   if v is not None and float(v)>1000: return float(v)
  except (TypeError,ValueError): pass
 return None
def extract_winner(obj):
 if not isinstance(obj,dict): return None
 status=str(obj.get("status") or obj.get("marketStatus") or "").upper()
 if "RESOLVED" not in status and not obj.get("resolved"): return None
 for key in ("winningOutcome","winning_outcome","winner","resolvedOutcome"):
  v=str(obj.get(key,"")).upper()
  if v in ("UP","DOWN"): return v
 return None
async def main():
 sem=asyncio.Semaphore(12)
 async with httpx.AsyncClient(timeout=18,follow_redirects=True,headers={"User-Agent":"btc-historical-research/1.0"}) as client:
  async def get(path):
   try:
    async with sem:
     r=await client.get(BASE+path)
     if r.status_code==200: return r.json(),None
     return None,"HTTP "+str(r.status_code)
   except Exception as e: return None,type(e).__name__
  async def one(i):
   t=START+timedelta(minutes=i*15)
   slug="btc-updown-15m-"+t.strftime("%Y-%m-%d-%H%Mz").lower()
   event,err=await get("/v1/events/slug/"+slug)
   e=event.get("event",event) if isinstance(event,dict) else {}
   markets=e.get("markets",[]) if isinstance(e,dict) else []
   if not markets and isinstance(event,dict): markets=event.get("markets",[])
   if not isinstance(markets,list): markets=[]
   market=next((x for x in markets if isinstance(x,dict) and slug in str(x.get("slug") or x.get("marketSlug") or "")),None)
   if market is None:
    market,err2=await get("/v1/markets/"+slug)
    if isinstance(market,dict): market=market.get("market",market)
    else: market=None
    if not market: err=err2 or err
   target=extract_target(e) or extract_target(market)
   outcome=extract_winner(market) or extract_winner(e)
   market_slug=market.get("slug") or market.get("marketSlug") if isinstance(market,dict) else None
   if market_slug and not outcome:
    detail,_=await get("/v1/markets/"+str(market_slug))
    if isinstance(detail,dict):
     detail=detail.get("market",detail)
     if isinstance(detail,dict): market={**(market or {}),**detail}
     outcome=extract_winner(market)
     target=target or extract_target(market)
   if market_slug and not outcome:
    # Reuse the same strict official payout mapping as live settlement.
    from app import verified_winner
    status=str((market or {}).get("status") or (market or {}).get("marketStatus") or "").upper()
    if "RESOLVED" in status:
     payout,_=await get("/v1/markets/"+str(market_slug)+"/settlement")
     outcome=verified_winner(market,payout)
   return {"utc_start":t.isoformat(),"event_slug":slug,"market_slug":market_slug,"price_to_beat":target,"winner":outcome,"status":"VERIFIED" if target is not None and outcome else "INCOMPLETE","error":err if market is None else None}
  rows=await asyncio.gather(*(one(i) for i in range(672)))
  # Coinbase public 1-minute OHLC: [epoch,low,high,open,close,volume].
  # Fetch overlapping <=300-minute windows; use only candles whose timestamps
  # fall in the round. Never fill missing candles with invented values.
  candles={}
  for chunk in range(0,7*24*60,290):
   start=START+timedelta(minutes=chunk)
   end=min(END,start+timedelta(minutes=290))
   try:
    async with sem:
     response=await client.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",params={"granularity":60,"start":start.isoformat(),"end":end.isoformat()},timeout=25)
    response.raise_for_status()
    for bar in response.json():
     if isinstance(bar,list) and len(bar)>=6:
      ts=int(bar[0])
      if START.timestamp()<=ts<END.timestamp():
       candles[ts]={"open":float(bar[3]),"high":float(bar[2]),"low":float(bar[1]),"close":float(bar[4]),"volume":float(bar[5])}
   except Exception as exc:
    print("Coinbase candle batch unavailable:",start.isoformat(),type(exc).__name__)
  for i,row in enumerate(rows):
   t=int((START+timedelta(minutes=i*15)).timestamp())
   bars=[candles.get(t+j*60) for j in range(15)]
   valid=[b for b in bars if b]
   row["candles_available"]=len(valid)
   row["movement_status"]="COMPLETE" if len(valid)==15 else "INCOMPLETE"
   row["open_spot"]=bars[0]["open"] if bars[0] else None
   row["close_spot"]=bars[-1]["close"] if bars[-1] else None
   row["high_spot"]=max(b["high"] for b in valid) if valid else None
   row["low_spot"]=min(b["low"] for b in valid) if valid else None
   row["spot_return_pct"]=round((bars[-1]["close"]/bars[0]["open"]-1)*100,5) if len(valid)==15 else None
   row["spot_direction"]="UP" if row["spot_return_pct"] is not None and row["spot_return_pct"]>0 else "DOWN" if row["spot_return_pct"] is not None and row["spot_return_pct"]<0 else None
   target=row["price_to_beat"]
   if target is not None and len(valid)==15:
    row["start_vs_target"]="ABOVE" if bars[0]["open"]>target else "BELOW" if bars[0]["open"]<target else "AT"
    row["end_vs_target"]="ABOVE" if bars[-1]["close"]>target else "BELOW" if bars[-1]["close"]<target else "AT"
    row["target_touched"]=any(b["low"]<=target<=b["high"] for b in bars)
    row["minutes_above_target"]=sum(b["close"]>target for b in bars)
    row["minutes_below_target"]=sum(b["close"]<target for b in bars)
    row["first_target_touch_minute"]=next((j for j,b in enumerate(bars) if b["low"]<=target<=b["high"]),None)
    row["target_crossings_at_close"]=sum((bars[j-1]["close"]-target)*(bars[j]["close"]-target)<0 for j in range(1,15))
   else:
    for key in ("start_vs_target","end_vs_target","target_touched","minutes_above_target","minutes_below_target","first_target_touch_minute","target_crossings_at_close"): row[key]=None
   # Minute-close reversal count (changes in the sign of consecutive minute returns).
   if len(valid)==15:
    signs=[1 if bars[j]["close"]>bars[j-1]["close"] else -1 if bars[j]["close"]<bars[j-1]["close"] else 0 for j in range(1,15)]
    signs=[s for s in signs if s]
    row["minute_direction_reversals"]=sum(signs[j]!=signs[j-1] for j in range(1,len(signs)))
   else: row["minute_direction_reversals"]=None
 Path("historical_15m_7d.json").write_text(json.dumps(rows,indent=2))
 with open("historical_15m_7d.csv","w",newline="") as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 print(json.dumps({"movement_complete":sum(r["movement_status"]=="COMPLETE" for r in rows),"target_movement_complete":sum(r["movement_status"]=="COMPLETE" and r["price_to_beat"] is not None for r in rows),"requested":672,"verified":sum(r["status"]=="VERIFIED" for r in rows),"targets":sum(r["price_to_beat"] is not None for r in rows),"winners":sum(r["winner"] is not None for r in rows),"missing":sum(r["status"]!="VERIFIED" for r in rows)}))
if __name__=="__main__": asyncio.run(main())

