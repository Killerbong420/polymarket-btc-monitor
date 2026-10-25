import asyncio, json, os, sqlite3, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
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
  with c.cursor() as cur:
   cur.execute(sql, params)
   if fetch: return cur.fetchall()
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
 r = await client.get("https://gamma-api.polymarket.com/markets",params={"active":"true","closed":"false","limit":100},timeout=12)
 r.raise_for_status()
 now = time.time()
 matches=[]
 for m in r.json():
  slug=str(m.get("slug",""))
  if not slug.startswith(os.getenv("MARKET_SLUG_PREFIX","btc-updown-5m")): continue
  try: end=datetime.fromisoformat(m["endDate"].replace("Z","+00:00")).timestamp()
  except (KeyError,ValueError,TypeError): continue
  if end>now and end-now<=600: matches.append((end,m))
 return min(matches,key=lambda x:x[0])[1] if matches else None
async def spot_data(client):
 errors=[]
 for host in ("https://api.binance.us","https://api.binance.com"):
  try:
   r=await client.get(host+"/api/v3/klines",params={"symbol":"BTCUSDT","interval":"1m","limit":30},timeout=10)
   r.raise_for_status()
   candles=r.json()
   if len(candles)<20: raise ValueError("insufficient candles")
   closes=[float(x[4]) for x in candles]
   vols=[float(x[5]) for x in candles]
   buy=[float(x[9]) for x in candles]
   recent=sum(vols[-5:])
   pressure=(2*sum(buy[-5:])/recent-1) if recent else 0
   momentum=(closes[-1]/closes[-6]-1)*100
   long_momentum=(closes[-1]/closes[-16]-1)*100
   volume_ratio=recent/(sum(vols[-20:-5])/3) if sum(vols[-20:-5]) else 1
   return {"price":closes[-1],"momentum_pct":round(momentum,4),"long_momentum_pct":round(long_momentum,4),"pressure":round(pressure,3),"volume_ratio":round(volume_ratio,2),"source":host}
  except Exception as e: errors.append(str(e))
 raise RuntimeError("External BTC feed unavailable: "+"; ".join(errors))
def judge(d):
 score=max(-2,min(2,d["momentum_pct"]*12))+max(-1,min(1,d["pressure"]*2))
 if d["volume_ratio"]>1.3: score*=1.15
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
async def settle(client):
 rows=execute("SELECT DISTINCT market FROM signals WHERE result='PENDING'",fetch=True)
 for (slug,) in rows[:30]:
  try:
   r=await client.get("https://gamma-api.polymarket.com/markets",params={"slug":slug},timeout=10)
   r.raise_for_status()
   markets=r.json()
   if not markets: continue
   w=winner(markets[0])
   if w:
    execute("UPDATE signals SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING' AND direction IN ('UP','DOWN')",(w,slug))
  except Exception: continue
async def loop():
 setup()
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
  while True:
   try:
    m=await market_data(client)
    d=await spot_data(client)
    direction,flip,score=judge(d)
    now=datetime.now(timezone.utc).isoformat()
    if not m:
     state.update(status="market unavailable",signal="WAIT",flip=flip,market=None,external=d,updated=now,error="No compatible active 5-minute market in Gamma results")
    else:
     slug=m["slug"]
     end=datetime.fromisoformat(m["endDate"].replace("Z","+00:00")).timestamp()
     remaining=end-time.time()
     phase="EARLY" if remaining>=180 else "LATE" if remaining<=90 else "MID"
     if phase in ("EARLY","LATE") and direction!="WAIT":
      execute("INSERT INTO signals (id,market,phase,direction,flip,score,created_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(slug+":"+phase,slug,phase,direction,flip,score,now))
     state.update(status="running",signal=direction,flip=flip,score=score,phase=phase,seconds_left=round(remaining),market=slug,external=d,updated=now,error=None)
    await settle(client)
   except Exception as e:
    state.update(status="feed error",signal="WAIT",flip="WAIT",error=str(e),updated=datetime.now(timezone.utc).isoformat())
   await asyncio.sleep(POLL)
@asynccontextmanager
async def lifespan(app):
 task=asyncio.create_task(loop())
 yield
 task.cancel()
 try: await task
 except asyncio.CancelledError: pass
app=FastAPI(lifespan=lifespan)
@app.get("/api/status")
def status():
 try:
  rows=execute("SELECT result,COUNT(*) FROM signals GROUP BY result",fetch=True)
  recent=execute("SELECT market,phase,direction,flip,score,created_at,result FROM signals ORDER BY created_at DESC LIMIT 50",fetch=True)
  return {**state,"counts":dict(rows),"history":[dict(zip(("market","phase","direction","flip","score","created_at","result"),r)) for r in recent]}
 except Exception as e: return {**state,"database_error":str(e),"history":[]}
@app.get("/",response_class=HTMLResponse)
def home():
 return """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC Monitor</title><style>body{background:#101723;color:#e7edf8;font:16px system-ui;margin:24px;max-width:850px}section{background:#202c3c;padding:18px;border-radius:14px;margin:12px 0}strong{font-size:28px}table{width:100%;font-size:12px}td,th{padding:8px;text-align:left;border-bottom:1px solid #465366}small{color:#b3c1d5}</style></head><body><h1>BTC 5-minute monitor</h1><small>Research only · No automatic orders · Uncalibrated signals</small><section><div id="status">Loading…</div><h2 id="market"></h2><strong id="signal">WAIT</strong><p id="phase"></p><h3 id="flip"></h3></section><section><h3>External BTC data</h3><pre id="external" style="white-space:pre-wrap"></pre></section><section><h3>Results</h3><p id="counts"></p><div style="overflow:auto"><table><thead><tr><th>Time UTC</th><th>Round</th><th>Phase</th><th>Call</th><th>Flip</th><th>Result</th></tr></thead><tbody id="history"></tbody></table></div></section><script>
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();for(const k of ['status','market','signal','flip'])document.getElementById(k).textContent=d[k]||'WAIT';document.getElementById('phase').textContent=(d.phase||'')+' '+(d.seconds_left??'')+' seconds remaining';document.getElementById('external').textContent=JSON.stringify(d.external||{},null,2);document.getElementById('counts').textContent=JSON.stringify(d.counts||{});document.getElementById('history').replaceChildren(...(d.history||[]).map(x=>{const tr=document.createElement('tr');for(const k of ['created_at','market','phase','direction','flip','result']){const td=document.createElement('td');td.textContent=x[k];tr.append(td)}return tr}));if(d.error)document.getElementById('status').textContent=d.status+': '+d.error}catch(e){document.getElementById('status').textContent='Connection error: '+e.message}}refresh();setInterval(refresh,15000)</script></body></html>"""
