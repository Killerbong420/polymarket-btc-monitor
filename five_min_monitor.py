"""Secondary, independently scored 5-minute Polymarket US BTC monitor.

Uses the same verified BRTI feed and official contract settlement as the
15-minute monitor; never changes its signals or historical statistics.
"""
import asyncio
import math
import time
from datetime import datetime, timezone, timedelta
from research import reference_prices, number
from settlement_forecast import forecast_settlement

state={"status":"starting","market":None,"direction":"WAIT"}
_last_market=None
POLL_SECONDS=5
VISUAL_REFRESH_SECONDS=15
VISUAL_MAX_POINTS=120
LATE_CHECKPOINT_MIN_LEFT=120
LATE_CHECKPOINT_MAX_LEFT=150

def eligible_late_checkpoint(seconds_left,early_created,end):
 """Capture the later forecast only near 2:30, never retrospectively."""
 return (LATE_CHECKPOINT_MIN_LEFT <= seconds_left <= LATE_CHECKPOINT_MAX_LEFT
         and early_created <= end-LATE_CHECKPOINT_MAX_LEFT)

def valid_terms(terms):
 """Reject incompatible terms even when the event supplies a positive target."""
 if not isinstance(terms,dict):return False
 target=number(terms.get("priceToBeat"))
 return (target is not None and target>0 and terms.get("indexSymbol")=="BRTI"
         and terms.get("marketType") in (None,"ASSET_PRICE_MARKET_TYPE_UP_DOWN"))

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
 async def fetch_event(event_slug):
  # Independent standard and CPC event lookups must not serialize two
  # eight-second network timeouts and miss the 2:30-left checkpoint.
  try:
   r=await client.get("https://gateway.polymarket.us/v1/events/slug/"+event_slug,timeout=8)
   if r.status_code!=200:
    return [],event_slug+":http_"+str(r.status_code)
   body=r.json()
   event=body.get("event",body) if isinstance(body,dict) else {}
   found=event.get("markets",[]) if isinstance(event,dict) else []
   if not found and isinstance(body,dict):found=body.get("markets",[])
   return (found if isinstance(found,list) else []),None
  except Exception as exc:
   return [],event_slug+":"+type(exc).__name__
 # gather preserves event priority: standard first, CPC second.
 for found,error in await asyncio.gather(fetch_event(slug),fetch_event("cpc-"+slug)):
  markets.extend(found)
  if error:errors.append(error)
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
 rows=(await asyncio.to_thread(execute, "SELECT market FROM five_min_predictions WHERE result='PENDING' ORDER BY created_epoch DESC LIMIT 24",fetch=True))
 late_rows=(await asyncio.to_thread(execute, "SELECT market FROM five_min_late_predictions WHERE result='PENDING' ORDER BY created_epoch DESC LIMIT 24",fetch=True))
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
    (await asyncio.to_thread(execute, f"UPDATE {table} SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END,winner=? WHERE market=? AND result='PENDING'",(winner,winner,slug)))
  except Exception:
   continue

def _counts(execute, table):
 # Only already-settled markets count. Each phase has its own last 100.
 rows=execute(f"SELECT result,COUNT(*) FROM (SELECT result FROM {table} WHERE result IN ('WIN','LOSS') ORDER BY created_epoch DESC LIMIT 100) t GROUP BY result",fetch=True)
 counts=dict(rows);wins=counts.get("WIN",0);losses=counts.get("LOSS",0);n=wins+losses
 return {"wins":wins,"losses":losses,"graded":n,"accuracy_pct":round(100*wins/n,1) if n else None}

def strict_late_counts(execute):
 """Grade only frozen 120-150s-left calls; audit recent capture timing.

 One bounded existing SQL read serves both the immutable scoreboard and
 a small diagnostic of why historical late rows do not qualify. The audit
 never reclassifies a call, changes its timestamp, or grades a pending row.
 """
 rows=execute("SELECT market,created_epoch,result FROM five_min_late_predictions WHERE result IN ('WIN','LOSS') ORDER BY created_epoch DESC LIMIT 1000",fetch=True)
 wins=losses=0
 audit={"sampled":min(len(rows),36),"within_window":0,"before_window":0,
        "after_window":0,"invalid_timestamp":0,"recent":[]}
 for idx,(slug,captured,result) in enumerate(rows):
  try:
   stamp=slug.split('btc-updown-5m-',1)[1]
   start=datetime.strptime(stamp,'%Y-%m-%d-%H%Mz').replace(tzinfo=timezone.utc)
   remaining=(start+timedelta(minutes=5)).timestamp()-float(captured)
   if not math.isfinite(remaining):raise ValueError("Non-finite capture time")
  except (ValueError,IndexError,TypeError,AttributeError,OverflowError):
   if idx<36:
    audit["invalid_timestamp"]+=1
    if idx<5:audit["recent"].append({"market":str(slug)[:90],"window":"invalid_timestamp"})
   continue
  if remaining>LATE_CHECKPOINT_MAX_LEFT:bucket="before_window"
  elif remaining<LATE_CHECKPOINT_MIN_LEFT:bucket="after_window"
  else:bucket="within_window"
  if idx<36:
   audit[bucket]+=1
   if idx<5:
    audit["recent"].append({"market":slug,"seconds_left":round(remaining,1),"window":bucket})
  if bucket!="within_window":continue
  if result=="WIN":wins+=1
  else:losses+=1
  if wins+losses>=100:break
 n=wins+losses
 return {"wins":wins,"losses":losses,"graded":n,
         "accuracy_pct":round(100*wins/n,1) if n else None,
         "capture_audit":audit}

def report(execute):
 early=_counts(execute,"five_min_predictions")
 recent=execute("SELECT market,direction,result,winner FROM five_min_predictions ORDER BY created_epoch DESC LIMIT 12",fetch=True)
 late=strict_late_counts(execute)
 late_legacy=_counts(execute,"five_min_late_predictions")
 late_recent=execute("SELECT market,direction,result,winner FROM five_min_late_predictions ORDER BY created_epoch DESC LIMIT 12",fetch=True)
 return {**early,"recent":[{"market":m,"direction":d,"result":r,"winner":w} for m,d,r,w in recent],
         "late":{**late,"recent":[{"market":m,"direction":d,"result":r,"winner":w} for m,d,r,w in late_recent],
                 "checkpoint_seconds_left":150,"capture_window_seconds_left":[120,150],
                 "basis":"Only independently graded frozen calls captured 120-150 seconds before expiry; historical off-window calls excluded"},
         "late_legacy":{**late_legacy,"basis":"Historical late calls captured with 20-150 seconds remaining; retained for audit only"},
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

def visual_forecast(reference, external, target, seconds_left, now):
 """Small, read-only official-index preview; never becomes a graded prediction.

 The final value is a forecast of the 60-second settlement *average*, not a
 future spot-price tick. No odds, contract pricing, or trade calculations.
 """
 unavailable={"status":"unavailable","reason":"Fresh verified BRTI projection unavailable"}
 if not isinstance(reference,dict) or reference.get("status")!="fresh" or reference.get("source")!="Polymarket US BTC reference / BRTI":
  return {**unavailable,"reason":"Fresh official BRTI reference required"}
 target=number(target);now=number(now)
 if target is None or target<=0 or now is None:
  return {**unavailable,"reason":"Verified official target required"}
 projection=forecast_settlement(reference,external,target,seconds_left,now=now)
 if projection.get("status")!="available":
  return {**unavailable,"reason":str(projection.get("reason") or "Insufficient observed reference history")}
 start=number(reference.get("window_start"))
 end=number(reference.get("window_end"))
 if start is None or end is None or abs((end-now)-seconds_left)>20 or not start<now<end:
  return {**unavailable,"reason":"Verified round timing required"}
 observed={}
 for item in reference.get("points") or []:
  if not isinstance(item,(list,tuple)) or len(item)!=2:continue
  t,p=number(item[0]),number(item[1])
  if t is not None and p is not None and p>0 and start<=t<=now and t<end:
   observed[int(t)]=float(p)
 ordered=sorted(observed.items())
 if not ordered:
  return {**unavailable,"reason":"No observed official BRTI price path"}
 stride=max(1,math.ceil(len(ordered)/VISUAL_MAX_POINTS))
 sampled=ordered[::stride]
 if sampled[-1]!=ordered[-1]:sampled.append(ordered[-1])
 final=number(projection.get("forecast_final_mean_usd"))
 current=number(projection.get("current_reference_usd"))
 low=number(projection.get("downside_scenario_usd"))
 high=number(projection.get("upside_scenario_usd"))
 if any(v is None or v<=0 for v in (final,current,low,high)):
  return {**unavailable,"reason":"Forecast numbers unavailable"}
 return {"status":"available","source":"Polymarket US BTC reference / BRTI",
         "as_of_epoch":round(now,3),"window_start_epoch":int(start),
         "window_end_epoch":int(end),"final_minute_start_epoch":int(end-60),
         "observed_points":sampled,"observed_ticks":len(ordered),
         "current_reference_usd":round(current,2),
         "official_target_usd":round(target,2),
         "projected_settlement_average_usd":round(final,2),
         "projected_gap_usd":round(final-target,2),
         "downside_scenario_usd":round(low,2),
         "upside_scenario_usd":round(high,2),
         "quality":projection.get("quality","LIMITED"),
         "reversal_warning":bool(projection.get("opposing_short_long_slopes")),
         "reference_age_seconds":projection.get("age_seconds"),
         "note":"Read-only projected final 60-second average; scenarios are uncalibrated. Observed prices are official BRTI ticks. Forecasts and historical grading remain frozen and separate."}

async def loop(client,execute,verified_winner,external_state):
 """Keep slow historical settlement requests out of the live forecast cadence."""
 (await asyncio.to_thread(setup, execute))
 async def score_worker():
  while True:
   try:
    state["settlement_task_running"]=True
    await score_pending(client,execute,verified_winner)
    state["settlement_task_error"]=None
   except asyncio.CancelledError:raise
   except Exception as exc:
    state["settlement_task_error"]=type(exc).__name__+": "+str(exc)[:150]
   finally:state["settlement_task_running"]=False
   await asyncio.sleep(35)
 scorer=asyncio.create_task(score_worker())
 try:
  await live_loop(client,execute,external_state)
 finally:
  scorer.cancel()
  await asyncio.gather(scorer,return_exceptions=True)

async def live_loop(client,execute,external_state):
 global _last_market
 last_visual_refresh=0.0
 while True:
  try:
   market,reason=await discover(client)
   if not market:
    state.update(status="unavailable",market=None,direction="WAIT",forecast=None,
                 early_direction="WAIT",late_direction="WAIT",phase="UNAVAILABLE",
                 target=None,visual_forecast={"status":"unavailable","reason":"No verified current round"},reason=reason)
   else:
    slug=market["slug"];left=max(0,market["end"]-time.time())
    terms=market.get("assetPriceTerms") or {}
    target=number(terms.get("priceToBeat"))
    # Event summaries may contain a target but omit the BRTI index identity.
    # Always verify full authoritative terms before forecasting.
    if not valid_terms(terms):
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
       if valid_terms(verified):
        target=candidate
        market["assetPriceTerms"]=verified
        break
      except Exception:continue
    valid_target=valid_terms(market.get("assetPriceTerms"))
    left=max(0,market["end"]-time.time())
    state.update(status="running" if valid_target else "target_unavailable",
                 market=slug,seconds_left=round(left),target=target if valid_target else None,
                 url="https://polymarket.us/event/"+slug)
    if slug!=_last_market:
     _last_market=slug
     last_visual_refresh=0.0
     state.update(visual_forecast={"status":"unavailable","reason":"Waiting for fresh BRTI"},
                  direction="WAIT",forecast=None,early_direction="WAIT",
                  early_forecast=None,late_direction="WAIT",late_forecast=None,
                  early_result=None,late_result=None,
                  phase="EARLY",result="PENDING",reason="Awaiting verified BRTI and official target")
    prior=(await asyncio.to_thread(execute, "SELECT direction,forecast,result,created_epoch FROM five_min_predictions WHERE market=?",(slug,),fetch=True))
    late_prior=(await asyncio.to_thread(execute, "SELECT direction,forecast,result,created_epoch FROM five_min_late_predictions WHERE market=?",(slug,),fetch=True))
    if prior:
     ed,ef,er,early_created=prior[0]
     state.update(direction=ed,forecast=ef,early_direction=ed,early_forecast=ef,
                  early_result=er,result=er,phase="EARLY",reason="Frozen early five-minute forecast")
    if late_prior:
     ld,lf,lr,late_created=late_prior[0]
     strict_late=bool(prior) and eligible_late_checkpoint(
      market["end"]-late_created,prior[0][3],market["end"])
     state.update(direction=ld,forecast=lf,late_direction=ld,
                  late_forecast=lf,late_result=lr,result=lr,
                  phase="LATE_2M30" if strict_late else "LEGACY_LATE",
                  reason=("Frozen 2:30-left forecast from newer verified BRTI ticks"
                          if strict_late else
                          "Legacy late forecast outside verified 2:30 window; excluded from strict accuracy"))
    if not valid_target:
     state.update(direction="WAIT",forecast=None,phase="UNVERIFIED_TARGET",
                  visual_forecast={"status":"unavailable","reason":"Official BRTI target not verified"},
                  reason="Official BRTI target and index terms unavailable")
    elif left>0:
     # Never fabricate a late checkpoint for a round first seen after 2:30.
     due_early=not prior and left>20
     due_late=(not late_prior and bool(prior) and
               eligible_late_checkpoint(left,prior[0][3],market["end"]))
     preview_due=time.time()-last_visual_refresh>=VISUAL_REFRESH_SECONDS
     if due_early or due_late or preview_due:
      reference=await reference_prices(client,market)
      captured=time.time()
      left=market["end"]-captured
      state["seconds_left"]=round(max(0,left))
      if preview_due or due_early or due_late:
       state["visual_forecast"]=visual_forecast(
        reference,external_state.get("external") or {},target,left,captured)
       last_visual_refresh=captured
      candidate,error=(None,"Forecast window closed during reference lookup")
      if left>20 and (due_early or (due_late and
                      eligible_late_checkpoint(left,prior[0][3],market["end"]))):
       candidate,error=forecast_candidate(reference,external_state.get("external") or {},target,left,captured)
      if candidate is None:
       # A visual-only refresh must not overwrite the frozen call explanation.
       if due_early or due_late:state["reason"]=error
      elif due_early:
       (await asyncio.to_thread(execute, "INSERT INTO five_min_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING",
               (slug,captured,candidate["direction"],target,candidate["forecast"])))
       state.update(direction=candidate["direction"],forecast=candidate["forecast"],
                    early_direction=candidate["direction"],early_forecast=candidate["forecast"],
                    phase="EARLY",result="PENDING",reason="Frozen original early BRTI forecast")
      elif due_late and eligible_late_checkpoint(left,prior[0][3],market["end"]):
       (await asyncio.to_thread(execute, "INSERT INTO five_min_late_predictions (market,created_epoch,direction,target,forecast) VALUES (?,?,?,?,?) ON CONFLICT(market) DO NOTHING",
               (slug,captured,candidate["direction"],target,candidate["forecast"])))
       state.update(direction=candidate["direction"],forecast=candidate["forecast"],
                    late_direction=candidate["direction"],late_forecast=candidate["forecast"],
                    phase="LATE_2M30",result="PENDING",
                    reason="Frozen 2:30-left BRTI forecast; independently scored")
   state["accuracy"]=(await asyncio.to_thread(report, execute))
  except asyncio.CancelledError:raise
  except Exception as e:state.update(status="error",direction="WAIT",forecast=None,reason=type(e).__name__+": "+str(e)[:150])
  state["updated_epoch"]=time.time()
  await asyncio.sleep(POLL_SECONDS)

