"""Collect 672 Polymarket US BTC 15-minute rounds. Never infer missing targets or winners.
Run: python historical_15m.py
Outputs: historical_15m_7d.json and historical_15m_7d.csv
"""
import asyncio,csv,json,re
from datetime import datetime,timedelta,timezone
from pathlib import Path
import httpx

BASE="https://gateway.polymarket.us"
END=datetime(2026,10,24,tzinfo=timezone.utc)
START=END-timedelta(days=7)
def extract_target(obj):
 if not isinstance(obj,dict): return None
 for key in ("priceToBeat","price_to_beat","targetPrice","target_price","strikePrice","strike_price"):
  v=obj.get(key)
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
   if not isinstance(markets,list): markets=[]
   market=next((x for x in markets if isinstance(x,dict)),None)
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
     outcome=extract_winner(detail)
     target=target or extract_target(detail)
   return {"utc_start":t.isoformat(),"event_slug":slug,"market_slug":market_slug,"price_to_beat":target,"winner":outcome,"status":"VERIFIED" if target is not None and outcome else "INCOMPLETE","error":err if market is None else None}
  rows=await asyncio.gather(*(one(i) for i in range(672)))
 Path("historical_15m_7d.json").write_text(json.dumps(rows,indent=2))
 with open("historical_15m_7d.csv","w",newline="") as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 print(json.dumps({"requested":672,"verified":sum(r["status"]=="VERIFIED" for r in rows),"targets":sum(r["price_to_beat"] is not None for r in rows),"winners":sum(r["winner"] is not None for r in rows),"missing":sum(r["status"]!="VERIFIED" for r in rows)}))
if __name__=="__main__": asyncio.run(main())
