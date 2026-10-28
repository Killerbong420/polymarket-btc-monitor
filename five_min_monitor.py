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
 execute("""CREATE TABLE IF NOT EXISTS five_min_late_predictions (
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
  # Event listings may omit assetPriceTerms. Validate identity by exact UTC round slug;
  # require BRTI terms from the authoritative market-detail response before forecasting.
  if not name.endswith(stamp):continue
  if terms and (terms.get("indexSymbol") not in (None,"BRTI") or terms.get("marketType") not in (None,"ASSET_PRICE_MARKET_TYPE_UP_DOWN")):continue
  return {**m,"slug":name,"end":(start+timedelta(minutes=5)).timestamp()},None
 return None,"no_verified_contract_or_metadata"

async def score_pending(client,execute,verified_winner):
 rows=execute("SELECT market FROM five_min_predictions WHERE result='PENDING' ORDER BY created_epoch DESC LIMIT 24",fetch=True)
 late_rows=execute("SELECT market FROM five_min_late_predictions WHERE result='PENDING' ORDER BY created_epoch DESC LIMIT 24",fetch=True)
 for (slug,) in dict.fromkeys(rows+late_rows):
  try:
   stamp=slug.split("btc-updown-5m-",1)[-1]
   start=datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
   if time.time()<(start+timedelta(minutes=6)).timestamp():continue
   match=None
   # Standard and CPC events are distinct on Polymarket US. A CPC
   # five-minute round must be graded against its own published event.
   for event in dict.fromkeys((slug, "btc-updown-5m-"+stamp, "cpc-btc-updown-5m-"+stamp)):
    er=await client.get("https://gateway.polymarket.us/v1/events/slug/"+event,timeout=8)
    if er.status_code!=200:continue
    body=er.json(); ev=body.get("event",body) if isinstance(body,dict) else {}
    markets=ev.get("markets",[]) if isinstance(ev,dict) else []
    if not markets and isinstance(body,dict):markets=body.get("markets",[])
    match=next((m for m in markets if isinstance(m,dict) and (m.get("slug") or m.get("marketSlug"))==slug),None)
    if match:break
   if not match:continue
   status=str(match.get("status") or match.get("marketStatus") or "").upper()
   if "RESOLVED" not in status or "UNRESOLVED" in status:continue
   sr=await client.get("https://gateway.polymarket.us/v1/markets/"+slug+"/settlement",timeout=8)
   if sr.status_code!=200:continue
   winner=verified_winner(match,sr.json())
   if winner not in ("UP","DOWN"):continue
   for table in ("five_min_predictions","five_min_late_predictions"):
    execute(f"UPDATE {table} SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END,winner=? WHERE market=? AND result='PENDING'",(winner,winner,slug))
  except Exception:
   continue

def _counts(execute, table):
 # Only already-settled markets count. Each phase has its own last 100.
 rows=execute(f"SELECT result,COUNT(*) FROM (SELECT result FROM {table} WHERE result IN ('WIN','LOSS') ORDER BY created_epoch DESC LIMIT 100) t GROUP BY result",fetch=True)
 counts=dict(rows);wins=counts.get("WIN",0);losses=counts.get("LOSS",0);n=wins+losses
 return {"wins":wins,"losses":losses,"graded":n,"accuracy_pct":round(100*wins/n,1) if n else None}

def report(execute):
 early=_counts(execute,"five_min_predictions")
 recent=execute("SELECT market,direction,result,winner FROM five_min_predictions ORDER BY created_epoch DESC LIMIT 12",fetch=True)
 late=_counts(execute,"five_min_late_predictions")
 late_recent=execute("SELECT market,direction,result,winner FROM five_min_late_predictions ORDER BY created_epoch DESC LIMIT 12",fetch=True)
 return {**early,"recent":[{"market":m,"direction":d,"result":r,"winner":w} for m,d,r,w in recent],
         "late":{**late,"recent":[{"market":m,"direction":d,"result":r,"winner":w} for m,d,r,w in late_recent],
                 "checkpoint_seconds_left":150,"basis":"Frozen calls captured with 20-150 seconds remaining; no backfill"},
         "early_basis":"Original first available frozen 5m predictions; historical records retained unchanged"}

def forecast_candidate(reference, external, target, left, now):
 """Same verified final-minute BRTI forecaster for early and 2:30 checkpoints."""
 if reference.get("status")!="fresh" or reference.get("source")!="Polymarket US BTC reference / BRTI":
  return None,"Official BRTI unavailable: "+str(reference.get("reason") or reference.get("status"))
 forecast=forecast_settlement(reference,external,target,left,now=now)
 if forecast.get("status")!="available":
  return None,"Forecast unavailable: "+str(forecast.get("reason") or "insufficient data")
 predicted=number(forecast.get("forecast_final_mean_usd"))
 if predicted is None or predicted<=0:
  return None,"Forecast numeric value unavailable"
 return {"direction":"UP" if predicted>=target else "DOWN","forecast":round(predicted,2)},None

async def loop(client,execute,verified_winner,external_state):
 global _last_market
 setup(execute)
 last_score=0
 while True:
  try:
   market,reason=await discover(client)
   if not market:
    state.update(status="unavailable",market=None,direction="WAIT",forecast=None,
                 early_direction="WAIT",late_direction="WAIT",phase="UNAVAILABLE",
                 target=None,reason=reason)
   else:
    slug=market["slug"];left=max(0,market["end"]-time.time())
    terms=market.get("assetPriceTerms") or {}
    target=number(terms.get("priceToBeat"))
    # Event summaries may contain a target but omit the BRTI index identity.
    # Always verify full authoritative terms before forecasting.
    if (target is None or target<=0 or terms.get("indexSymbol")!="BRTI"
            or terms.get("marketType") not in (None,"ASSET_PRICE_MARKET_TYPE_UP_DOWN")):
     for path in ("/v1/markets/"+slug,"/v1/market/slug/"+slug):
      try:
       response=await client.get("https://gateway.polymarket.us"+path,timeout=6)
       if response.status_code!=200:continue
       body=response.json()
       detail=body.get("market",body) if isinstance(body,dict) else {}
       if not isinstance(detail,dict):continue
       if (detail.get("slug") or detail.get("marketSlug"))!=slug:continue
       verified=detail.get("assetPriceTerms") or {}
       candidate=number(verified.get("priceToBeat"))
       if (verified.get("indexSymbol")=="BRTI" and
           verified.get("marketType") in (None,"ASSET_PRICE_MARKET_TYPE_UP_DOWN")
           and candidate is not None and candidate>0):
        target=candidate
        market["assetPriceTerms"]=verified
        break
      except Exception:continue
    valid_target=(target is not None and target>0 and
                  (market.get("assetPriceTerms") or {}).get("indexSymbol")=="BRTI")
    state.update(status="running" if valid_target else "target_unavailable",
                 market=slug,seconds_left=round(left),target=target if valid_target else None,
                 url="https://polymarket.us/event/"+slug)
    if slug!=_last_market:
     _last_market=slug
     state.update(direction="WAIT",forecast=None,early_direction="WAIT",
                  early_forecast=None,late_direction="WAIT",late_forecast=None,
                  phase="EARLY",result="PENDING",reason="Awaiting verified BRTI and official target")
    prior=execute("SELECT direction,forecast,result,created_epoch FROM five_min_predictions WHERE market=?",(slug,),fetch=True)
    late_prior=execute("SELECT direction,forecast,result FROM five_min_late_predictions WHERE market=?",(slug,),fetch=True)
    if prior:
     ed,ef,er,early_created=prior[0]
     state.update(direction=ed,forecast=ef,early_direction=ed,early_forecast=ef,
                  early_result=er,result=er,phase="EARLY",reason="Frozen early five-minute forecast")
    if late_prior:
     ld,lf,lr=late_prior[0]
     state.update(direction=ld,forecast=lf,late_direction=ld,
                  late_forecast=lf,late_result=lr,result=lr,phase="LATE_2M30",
                  reason="Frozen 2:30-left forecast from newer verified BRTI ticks")
    if not valid_target:
     state.update(direction="WAIT",forecast=None,phase="UNVERIFIED_TARGET",
                  reason="Official BRTI target and index terms unavailable")
    elif left>20:
     # Never fabricate a late checkpoint for a round first seen after 2:30.
     due_early=not prior
     due_late=(not late_prior and bool(prior) and 20<left<=150
               and prior[0][3] <= market["end"]-150)
     if due_early or due_late:
      reference=await reference_prices(client,market)
      candidate,error=forecast_candidate(reference,external_state.get("external") or {},target,left,time.time())
      if candidate is None:
       state["reason"]=error
      elif due_early:
       execute("INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING",
               (slug,time.time(),candidate["direction"],target,candidate["forecast"]))
       state.update(direction=candidate["direction"],forecast=candidate["forecast"],
                    early_direction=candidate["direction"],early_forecast=candidate["forecast"],
                    phase="EARLY",result="PENDING",reason="Frozen original early BRTI forecast")
      elif due_late:
       execute("INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING",
               (slug,time.time(),candidate["direction"],target,candidate["forecast"]))
       state.update(direction=candidate["direction"],forecast=candidate["forecast"],
                    late_direction=candidate["direction"],late_forecast=candidate["forecast"],
                    phase="LATE_2M30",result="PENDING",
                    reason="Frozen 2:30-left BRTI forecast; independently scored")
   if time.monotonic()-last_score>=35:
    await score_pending(client,execute,verified_winner)
    last_score=time.monotonic()
   state["accuracy"]=report(execute)
  except asyncio.CancelledError:raise
  except Exception as e:state.update(status="error",reason=type(e).__name__+": "+str(e)[:150])
  await asyncio.sleep(5)
