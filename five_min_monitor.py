"""Secondary, independently scored 5-minute Polymarket US BTC monitor.

Uses the same verified BRTI feed and official contract settlement as the
15-minute monitor; never changes its signals or historical statistics.
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from research import reference_prices, number
from settlement_forecast import forecast_settlement

state={"status":"starting","market":None,"direction":"WAIT"}
_last_market=None

def setup(execute):
 execute("""CREATE TABLE IF NOT EXISTS five_min_predictions (
 market TEXT PRIMARY KEY, created_epoch REAL NOT NULL,
 direction TEXT NOT NULL, target REAL NOT NULL, forecast REAL NOT NULL,
 result TEXT NOT NULL DEFAULT 'PENDING', winner TEXT)""")

async def discover(client):
 now=datetime.now(timezone.utc)
 start=now.replace(minute=(now.minute//5)*5,second=0,microsecond=0)
 stamp=start.strftime("%Y-%m-%d-%H%Mz").lower()
 slug="btc-updown-5m-"+stamp
 markets=[]
 errors=[]
 for event_slug in (slug,"cpc-"+slug):
  try:
   r=await client.get("https://gateway.polymarket.us/v1/events/slug/"+event_slug,timeout=8)
   if r.status_code!=200:
    errors.append(event_slug+":http_"+str(r.status_code))
    continue
   body=r.json()
   event=body.get("event",body) if isinstance(body,dict) else {}
   found=event.get("markets",[]) if isinstance(event,dict) else []
   if not found and isinstance(body,dict):found=body.get("markets",[])
   if isinstance(found,list):markets.extend(found)
  except Exception as exc:
   errors.append(event_slug+":"+type(exc).__name__)
 if not markets:return None,"; ".join(errors) or "event_has_no_markets"
 for m in markets if isinstance(markets,list) else []:
  if not isinstance(m,dict):continue
  name=m.get("slug") or m.get("marketSlug") or ""
  if "btc-updown-5m-" not in name:continue
  terms=m.get("assetPriceTerms") or {}
  asset=terms.get("asset") or {}
  if terms.get("marketType")!="ASSET_PRICE_MARKET_TYPE_UP_DOWN" or terms.get("indexSymbol")!="BRTI" or str(asset.get("symbol","")).lower()!="btc":continue
  return {**m,"slug":name,"end":(start+timedelta(minutes=5)).timestamp()},None
 return None,"no_verified_contract_or_metadata"

async def score_pending(client,execute,verified_winner):
 rows=execute("SELECT market FROM five_min_predictions WHERE result='PENDING' ORDER BY created_epoch DESC LIMIT 24",fetch=True)
 for (slug,) in rows:
  try:
   stamp=slug.split("btc-updown-5m-",1)[-1]
   start=datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
   if time.time()<(start+timedelta(minutes=6)).timestamp():continue
   event="btc-updown-5m-"+stamp
   er=await client.get("https://gateway.polymarket.us/v1/events/slug/"+event,timeout=8)
   if er.status_code!=200:continue
   body=er.json(); ev=body.get("event",body) if isinstance(body,dict) else {}
   markets=ev.get("markets",[]) if isinstance(ev,dict) else []
   if not markets and isinstance(body,dict):markets=body.get("markets",[])
   match=next((m for m in markets if isinstance(m,dict) and (m.get("slug") or m.get("marketSlug"))==slug),None)
   if not match:continue
   status=str(match.get("status") or match.get("marketStatus") or "").upper()
   if "RESOLVED" not in status or "UNRESOLVED" in status:continue
   sr=await client.get("https://gateway.polymarket.us/v1/markets/"+slug+"/settlement",timeout=8)
   if sr.status_code!=200:continue
   winner=verified_winner(match,sr.json())
   if winner not in ("UP","DOWN"):continue
   execute("UPDATE five_min_predictions SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END,winner=? WHERE market=? AND result='PENDING'",(winner,winner,slug))
  except Exception:
   continue

def report(execute):
 rows=execute("SELECT result,COUNT(*) FROM (SELECT result FROM five_min_predictions WHERE result IN ('WIN','LOSS') ORDER BY created_epoch DESC LIMIT 100) t GROUP BY result",fetch=True)
 counts=dict(rows);wins=counts.get("WIN",0);losses=counts.get("LOSS",0);n=wins+losses
 recent=execute("SELECT market,direction,result,winner FROM five_min_predictions ORDER BY created_epoch DESC LIMIT 12",fetch=True)
 return {"wins":wins,"losses":losses,"graded":n,"accuracy_pct":round(100*wins/n,1) if n else None,"recent":[{"market":m,"direction":d,"result":r,"winner":w} for m,d,r,w in recent]}

async def loop(client,execute,verified_winner,external_state):
 global _last_market
 setup(execute)
 last_score=0
 while True:
  try:
   market,reason=await discover(client)
   if not market:
    state.update(status="unavailable",market=None,direction="WAIT",reason=reason)
   else:
    slug=market["slug"];left=max(0,market["end"]-time.time())
    target=number((market.get("assetPriceTerms") or {}).get("priceToBeat"))
    state.update(status="running" if target is not None and target>0 else "target_unavailable",market=slug,seconds_left=round(left),target=target,
                 url="https://polymarket.us/event/"+slug)
    if slug!=_last_market:
     _last_market=slug
     state.update(direction="WAIT",forecast=None,reason="Awaiting verified BRTI and official target")
    prior=execute("SELECT direction,forecast,result FROM five_min_predictions WHERE market=?",(slug,),fetch=True)
    if prior:
     direction,forecast,result=prior[0]
     state.update(direction=direction,forecast=forecast,result=result,reason="Frozen prospective five-minute settlement forecast")
    elif target is None or target<=0:
     state.update(direction="WAIT",forecast=None,reason="Official priceToBeat absent in event contract; requesting market details")
     for path in ("/v1/markets/"+slug,"/v1/market/slug/"+slug):
      try:
       response=await client.get("https://gateway.polymarket.us"+path,timeout=6)
       if response.status_code!=200:continue
       body=response.json()
       detail=body.get("market",body) if isinstance(body,dict) else {}
       if not isinstance(detail,dict):continue
       if (detail.get("slug") or detail.get("marketSlug"))!=slug:continue
       terms=detail.get("assetPriceTerms") or {}
       candidate=number(terms.get("priceToBeat"))
       if terms.get("indexSymbol")=="BRTI" and candidate is not None and candidate>0:
        target=candidate
        state.update(target=target,status="running",reason="Official target retrieved from market details")
        break
      except Exception:continue
    if not prior and target is not None and target>0 and left>20:
     reference=await reference_prices(client,market)
     if reference.get("status")!="fresh":state["reason"]="Official BRTI unavailable: "+str(reference.get("reason") or reference.get("status"))
     if reference.get("status")=="fresh" and reference.get("source")=="Polymarket US BTC reference / BRTI":
      forecast=forecast_settlement(reference,external_state.get("external") or {},target,left,now=time.time())
      predicted=number(forecast.get("forecast_final_mean_usd"))
      if forecast.get("status")!="available":state["reason"]="Forecast unavailable: "+str(forecast.get("reason") or "insufficient data")
      if forecast.get("status")=="available" and predicted is not None and predicted>0:
       direction="UP" if predicted>=target else "DOWN"
       execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING",(slug,time.time(),direction,target,predicted))
       state.update(direction=direction,forecast=round(predicted,2),result="PENDING",reason="Observed BRTI forward settlement forecast")
   if time.monotonic()-last_score>=35:
    await score_pending(client,execute,verified_winner)
    last_score=time.monotonic()
   state["accuracy"]=report(execute)
  except asyncio.CancelledError:raise
  except Exception as e:state.update(status="error",reason=type(e).__name__)
  await asyncio.sleep(5)
