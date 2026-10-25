import asyncio, json, os, sqlite3, time
from pathlib import Path
from historical_15m import main as collect_15m
from ai_confirmation import confirm, public_state, check_auth, auth_state
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
try:
 import psycopg
except ImportError:
 psycopg = None

POLL = max(10, int(os.getenv("POLL_SECONDS", "15")))
DB = os.getenv("DATABASE_URL", "")
state = {"status":"starting","signal":"WAIT","flip":"WAIT","market":None,"updated":None,"error":None}
def connect():
 if DB:
  return psycopg.connect(DB)
 return sqlite3.connect("monitor.db")
def execute(sql, params=(), fetch=False):
 with connect() as c:
  if DB: sql = sql.replace("?", "%s")
  cur = c.cursor()
  try:
   cur.execute(sql, params)
   if fetch: return cur.fetchall()
  finally:
   cur.close()
def setup():
 execute("""CREATE TABLE IF NOT EXISTS signals (
 id TEXT PRIMARY KEY, market TEXT NOT NULL, phase TEXT NOT NULL,
 direction TEXT NOT NULL, flip TEXT NOT NULL, score REAL NOT NULL,
 created_at TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'PENDING')""")
def parse_json(v):
 if isinstance(v,list): return v
 try: return json.loads(v)
 except (ValueError,TypeError): return []
async def market_data(client):
 # Polymarket US and international Polymarket are different venues.
 # Never substitute international Gamma markets for US contracts.
 base="https://gateway.polymarket.us"
 now=datetime.now(timezone.utc)
 floor=now.replace(minute=(now.minute//5)*5,second=0,microsecond=0)
 errors=[]
 # US five-minute event slugs use UTC date and HHMMz.
 for delta in (0,5,10,-5):
  from datetime import timedelta
  t=floor+timedelta(minutes=delta)
  slug="btc-updown-5m-"+t.strftime("%Y-%m-%d-%H%Mz").lower()
  if t.timestamp()+300<=time.time(): continue
  # Polymarket US event slug differs from its contract market slug.
  # Resolve event first and require a real market record in its markets array.
  try:
   er=await client.get(base+"/v1/events/slug/"+slug,timeout=7)
   if er.status_code==200:
    body=er.json()
    event=body.get("event",body) if isinstance(body,dict) else {}
    contracts=event.get("markets",[]) if isinstance(event,dict) else []
    if not contracts and isinstance(body,dict): contracts=body.get("markets",[])
    if isinstance(contracts,list):
     for contract in contracts:
      if not isinstance(contract,dict): continue
      market_slug=contract.get("slug") or contract.get("marketSlug")
      if not isinstance(market_slug,str) or not market_slug: continue
      contract=dict(contract)
      contract["slug"]=market_slug
      contract["_event_slug"]=slug
      contract["_round_start"]=t.timestamp()
      contract["_round_end"]=(t+timedelta(minutes=5)).timestamp()
      return contract
  except Exception as exc: errors.append("event lookup "+type(exc).__name__+": "+str(exc)[:90])
  # A successful market lookup must correspond to this exact US round.
  for path in ("/v1/markets/"+slug, "/v1/market/slug/"+slug):
   try:
    r=await client.get(base+path,timeout=7)
    if r.status_code==404: continue
    r.raise_for_status()
    data=r.json()
    market=data.get("market",data) if isinstance(data,dict) else None
    if isinstance(market,dict) and (market.get("slug")==slug or market.get("marketSlug")==slug):
     market["_round_start"]=t.timestamp()
     market["_round_end"]=(t+timedelta(minutes=5)).timestamp()
     market["slug"]=slug
     return market
   except Exception as exc: errors.append(type(exc).__name__+": "+str(exc)[:110])
 # Discover available US markets without guessing their response structure.
 try:
  r=await client.get(base+"/v1/markets",params={"limit":500,"offset":0},timeout=12)
  r.raise_for_status()
  payload=r.json()
  items=payload if isinstance(payload,list) else payload.get("markets",[]) if isinstance(payload,dict) else []
  candidates=[]
  for m in items:
   if not isinstance(m,dict): continue
   slug=str(m.get("slug") or m.get("marketSlug") or "")
   if not slug.startswith("btc-updown-5m-"): continue
   try:
    stamp=slug[len("btc-updown-5m-"):]
    t=datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
    remaining=(t.timestamp()+300)-time.time()
    if 0<remaining<=600:
     m["slug"]=slug
     m["_round_start"]=t.timestamp()
     m["_round_end"]=t.timestamp()+300
     candidates.append((abs(remaining-150),m))
   except ValueError: continue
  if candidates: return min(candidates,key=lambda x:x[0])[1]
  # The first page is often unrelated sports markets. Search the US catalog
  # rather than assuming 500 generic results represent all available markets.
  search_details=[]
  for term in ("btc-updown-5m","bitcoin"):
   try:
    sr=await client.get(base+"/v1/search",params={"query":term},timeout=12)
    sr.raise_for_status()
    body=sr.json()
    found=[]
    def scan(node,depth=0):
     if depth>5: return
     if isinstance(node,dict):
      slug=str(node.get("slug") or node.get("marketSlug") or "")
      if slug.startswith("btc-updown-5m-"): found.append(node)
      for v in node.values():
       if isinstance(v,(dict,list)): scan(v,depth+1)
     elif isinstance(node,list):
      for v in node[:200]: scan(v,depth+1)
    scan(body)
    search_details.append({"query":term,"status":sr.status_code,"btc_matches":len(found)})
    for m in found:
     slug=str(m.get("slug") or m.get("marketSlug"))
     try:
      t=datetime.strptime(slug[len("btc-updown-5m-"):],"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
      remaining=t.timestamp()+300-time.time()
      if 0<remaining<=600:
       m["slug"]=slug
       m["_round_start"]=t.timestamp()
       m["_round_end"]=t.timestamp()+300
       return m
     except ValueError: continue
   except Exception as exc: search_details.append({"query":term,"error":str(exc)[:160]})
  state["market_diagnostic"]={"venue":"Polymarket US","gateway_status":r.status_code,"markets_scanned":len(items),"sample_slugs":[str(m.get("slug") or m.get("marketSlug")) for m in items[:3] if isinstance(m,dict)],"search":search_details,"lookup_errors":errors[-3:]}
 except Exception as exc:
  state["market_diagnostic"]={"venue":"Polymarket US","error":str(exc)[:300],"lookup_errors":errors[-3:]}
 return None
async def spot_data(client):
 errors=[]
 # Coinbase public BTC-USD candles: [timestamp, low, high, open, close, volume].
 # No taker-buy breakdown: pressure is neutral, not fabricated.
 try:
  r=await client.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",params={"granularity":60},timeout=12)
  r.raise_for_status()
  raw=r.json()
  candles=sorted([x for x in raw if isinstance(x,list) and len(x)>=6],key=lambda x:x[0])
  # Exclude the current incomplete minute.
  candles=[x for x in candles if float(x[0])+60<=time.time()]
  if len(candles)<21: raise ValueError("Coinbase insufficient completed candles")
  candles=candles[-30:]
  closes=[float(x[4]) for x in candles]
  vols=[float(x[5]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Coinbase BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-6]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"source":"Coinbase Exchange BTC-USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
 except Exception as e: errors.append("Coinbase: "+str(e)[:150])
 # Kraken OHLC uses BTC/USD minute candles; last candle is incomplete.
 try:
  r=await client.get("https://api.kraken.com/0/public/OHLC",params={"pair":"XBTUSD","interval":1},timeout=12)
  r.raise_for_status()
  payload=r.json()
  if payload.get("error"): raise ValueError(str(payload["error"]))
  key=next(k for k in payload["result"] if k!="last")
  candles=sorted(payload["result"][key],key=lambda x:float(x[0]))[:-1]
  if len(candles)<21: raise ValueError("Kraken insufficient candles")
  candles=candles[-30:]
  closes=[float(x[4]) for x in candles]
  vols=[float(x[6]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Kraken BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-6]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"source":"Kraken BTC/USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
 except Exception as e: errors.append("Kraken: "+str(e)[:150])
 raise RuntimeError("External BTC feed unavailable: "+"; ".join(errors))
def judge(d):
 score=max(-2,min(2,d["momentum_pct"]*12))+(max(-1,min(1,d["pressure"]*2)) if d.get("pressure_available",True) else 0)
 if d["volume_ratio"]>1.3: score*=1.15
 if d.get("candle_age_seconds",0)>180: return "WAIT","WAIT",0.0
 direction="UP" if score>0.6 else "DOWN" if score< -0.6 else "WAIT"
 flip="POSSIBLE UP FLIP" if d["long_momentum_pct"]< -0.06 and d["momentum_pct"]>0.025 and d["pressure"]>0.08 else "POSSIBLE DOWN FLIP" if d["long_momentum_pct"]>0.06 and d["momentum_pct"]< -0.025 and d["pressure"]< -0.08 else "WAIT"
 return direction,flip,round(score,3)
def winner(m):
 outcomes=parse_json(m.get("outcomes"))
 prices=parse_json(m.get("outcomePrices"))
 if not m.get("closed") or len(outcomes)!=2 or len(prices)!=2: return None
 try: p=[float(x) for x in prices]
 except (TypeError,ValueError): return None
 if sorted(p)!=[0.0,1.0]: return None
 w=str(outcomes[p.index(1.0)]).upper()
 return w if w in ("UP","DOWN") else None
def settlement_price(data):
 # Polymarket US settlement is a YES-side payout (0 or 1), not an UP/DOWN label.
 if isinstance(data,(int,float)) and not isinstance(data,bool): return float(data)
 if not isinstance(data,dict): return None
 for obj in (data,data.get("settlement"),data.get("data")):
  if not isinstance(obj,dict): continue
  for key in ("settlementPrice","settlement_price","price","value"):
   if key in obj and obj[key] is not None:
    try: return float(obj[key])
    except (TypeError,ValueError): pass
 return None

def verified_winner(m,settlement):
 # Only resolve if the contract explicitly identifies the long/YES outcome as UP or DOWN.
 # Never align outcomePrices with outcomes by array index.
 if not isinstance(m,dict): return None
 status=str(m.get("status") or m.get("marketStatus") or "").upper()
 if "RESOLVED" not in status: return None
 sides=m.get("marketSides") or m.get("market_sides")
 if not isinstance(sides,list): return None
 yes=None
 for side in sides:
  if not isinstance(side,dict): continue
  position=str(side.get("side") or side.get("position") or side.get("type") or "").upper()
  label=str(side.get("outcome") or side.get("name") or side.get("label") or "").upper()
  if position in ("LONG","YES","MARKET_SIDE_LONG") and label in ("UP","DOWN"):
   yes=label
 if yes is None: return None
 p=settlement_price(settlement)
 if p==1.0: return yes
 if p==0.0: return "DOWN" if yes=="UP" else "UP"
 return None

async def settle(client):
 pending=execute("SELECT DISTINCT market FROM signals WHERE result='PENDING'",fetch=True)
 diagnostic={"pending_markets":len(pending),"checked":0,"verified":0,"unresolved":0,"errors":[],"reasons":{}}
 def unresolved(reason):
  diagnostic["unresolved"]+=1
  diagnostic["reasons"][reason]=diagnostic["reasons"].get(reason,0)+1
 for (slug,) in pending[:20]:
  try:
   base="https://gateway.polymarket.us/v1/markets/"+slug
   diagnostic["checked"]+=1
   mr=await client.get(base,timeout=8)
   if mr.status_code!=200:
    unresolved("market_http_"+str(mr.status_code))
    continue
   payload=mr.json()
   market=payload.get("market",payload) if isinstance(payload,dict) else None
   if not isinstance(market,dict):
    unresolved("market_payload_invalid")
    continue
   status=str(market.get("status") or market.get("marketStatus") or "").upper()
   if "RESOLVED" not in status:
    unresolved("market_not_resolved_"+(status or "unknown")[:35])
    continue
   sr=await client.get(base+"/settlement",timeout=8)
   if sr.status_code!=200:
    unresolved("settlement_http_"+str(sr.status_code))
    continue
   win=verified_winner(market,sr.json())
   if win is None:
    unresolved("resolved_but_winner_mapping_unverified")
    continue
   execute("UPDATE signals SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING'",(win,slug))
   diagnostic["verified"]+=1
  except Exception as exc:
   diagnostic["errors"].append(type(exc).__name__+": "+str(exc)[:100])
 state["settlement_diagnostic"]=diagnostic
async def loop():
 setup()
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
  while True:
   try:
    await check_auth(client)
    m=await market_data(client)
    d=await spot_data(client)
    direction,flip,score=judge(d)
    now=datetime.now(timezone.utc).isoformat()
    if not m:
     for key in ("phase","seconds_left","score"): state.pop(key,None)
     state.update(status="market unavailable",signal="WAIT",flip=flip,market=None,external=d,updated=now,error="No compatible Polymarket US 5-minute market found; see market_diagnostic")
    else:
     slug=m["slug"]
     end=m["_round_end"]
     remaining=end-time.time()
     phase="EARLY" if remaining>=180 else "LATE" if remaining<=90 else "MID"
     state.pop("market_diagnostic",None)
     if phase in ("EARLY","LATE") and direction!="WAIT":
      execute("INSERT INTO signals (id,market,phase,direction,flip,score,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(slug+":"+phase,slug,phase,direction,flip,score,now))
     state.update(status="running",signal=direction,flip=flip,score=score,phase=phase,seconds_left=round(remaining),market=slug,external=d,updated=now,error=None)
     await confirm(client,slug,phase,direction,flip,score,d)
    await settle(client)
   except Exception as e:
    for key in ("phase","seconds_left","score"): state.pop(key,None)
    state.update(status="feed error",signal="WAIT",flip="WAIT",error=str(e),updated=datetime.now(timezone.utc).isoformat())
   await asyncio.sleep(POLL)
@asynccontextmanager
async def lifespan(app):
 task=asyncio.create_task(loop())
 async def historical_job():
  try:
   historical_state.update(status="running",started=datetime.now(timezone.utc).isoformat())
   await collect_15m()
   records=json.loads(Path("historical_15m_7d.json").read_text())
   historical_state.update(status="complete",checked=len(records),targets=sum(x["price_to_beat"] is not None for x in records),winners=sum(x["winner"] is not None for x in records),verified=sum(x["status"]=="VERIFIED" for x in records),movement_complete=sum(x.get("movement_status")=="COMPLETE" for x in records),target_movement_complete=sum(x.get("movement_status")=="COMPLETE" and x.get("price_to_beat") is not None for x in records),finished=datetime.now(timezone.utc).isoformat())
  except Exception as exc: historical_state.update(status="error",error=str(exc)[:300])
 history_task=asyncio.create_task(historical_job())
 yield
 task.cancel()
 history_task.cancel()
 for running in (task,history_task):
  try: await running
  except asyncio.CancelledError: pass
historical_state={"status":"pending","requested":672,"period":"2026-10-17 through 2026-10-24 UTC","verified":0}
app=FastAPI(lifespan=lifespan)
@app.get("/api/historical-15m/status")
def historical_status():
 return historical_state
@app.get("/api/historical-15m/results")
def historical_results():
 p=Path("historical_15m_7d.json")
 if not p.exists(): return JSONResponse({"status":historical_state,"records":[]},status_code=202)
 return {"status":historical_state,"records":json.loads(p.read_text())}
@app.get("/api/historical-15m/csv")
def historical_csv():
 p=Path("historical_15m_7d.csv")
 if not p.exists(): return JSONResponse({"status":historical_state},status_code=202)
 return FileResponse(str(p),media_type="text/csv",filename="polymarket_us_btc_15m_7d.csv")
@app.get("/api/status")
def status():
 try:
  rows=execute("SELECT result,COUNT(*) FROM signals GROUP BY result",fetch=True)
  recent=execute("SELECT market,phase,direction,flip,score,created_at,result FROM signals ORDER BY created_at DESC LIMIT 50",fetch=True)
  return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"counts":dict(rows),"history":[dict(zip(("market","phase","direction","flip","score","created_at","result"),r)) for r in recent]}
 except Exception as e: return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"database_error":str(e),"history":[]}
@app.get("/",response_class=HTMLResponse)
def home():
 return Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
