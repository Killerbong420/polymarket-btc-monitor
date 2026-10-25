import asyncio, json, os, sqlite3, time
from pathlib import Path
from historical_15m import main as collect_15m
from ai_confirmation import confirm, public_state, check_auth, auth_state, halfway_decision
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
try:
 import psycopg
except ImportError:
 psycopg = None

POLL = max(5, int(os.getenv("POLL_SECONDS", "5")))
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
 for col in ("quality TEXT NOT NULL DEFAULT 'UNRATED'","quality_reason TEXT NOT NULL DEFAULT 'Legacy prediction; quality not captured'","decision_source TEXT NOT NULL DEFAULT 'LEGACY'","decision_reason TEXT NOT NULL DEFAULT ''","ai_confidence TEXT NOT NULL DEFAULT 'UNRATED'"):
  try: execute("ALTER TABLE signals ADD COLUMN "+col)
  except Exception as exc:
   if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower(): raise
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
async def market_15m(client):
 # Only accept a real Polymarket US contract returned for the current 15m event.
 from datetime import timedelta
 now=datetime.now(timezone.utc)
 start=now.replace(minute=(now.minute//15)*15,second=0,microsecond=0)
 event_slug="btc-updown-15m-"+start.strftime("%Y-%m-%d-%H%Mz").lower()
 try:
  response=await client.get("https://gateway.polymarket.us/v1/events/slug/"+event_slug,timeout=8)
  if response.status_code!=200: return None,"event_http_"+str(response.status_code)
  body=response.json()
  event=body.get("event",body) if isinstance(body,dict) else {}
  markets=event.get("markets",[]) if isinstance(event,dict) else []
  if not markets and isinstance(body,dict): markets=body.get("markets",[])
  for market in markets if isinstance(markets,list) else []:
   if not isinstance(market,dict): continue
   slug=market.get("slug") or market.get("marketSlug")
   if not isinstance(slug,str) or "btc-updown-15m-" not in slug: continue
   return {**market,"slug":slug,"end":(start+timedelta(minutes=15)).timestamp()},None
  return None,"event_has_no_matching_15m_contract"
 except Exception as exc: return None,type(exc).__name__

def chart_indicators(candles, volume_idx):
 closes=[float(x[4]) for x in candles]
 highs=[float(x[2]) for x in candles]
 lows=[float(x[1]) for x in candles]
 def ema(values,n):
  result=values[0]
  for v in values[1:]: result+=(2/(n+1))*(v-result)
  return result
 def rsi(values,n=14):
  changes=[values[i]-values[i-1] for i in range(1,len(values))]
  gains=sum(max(0,x) for x in changes[-n:])/n
  losses=sum(max(0,-x) for x in changes[-n:])/n
  return 100 if losses==0 and gains>0 else 50 if losses==0 else 100-100/(1+gains/losses)
 e9=ema(closes[-25:],9)
 e21=ema(closes[-35:],21)
 last=closes[-1]
 ranges=[max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1])) for i in range(1,len(closes))]
 atr=sum(ranges[-14:])/14
 return {"ema9":round(e9,2),"ema21":round(e21,2),"rsi14":round(rsi(closes),1),"atr14_pct":round(100*atr/last,4),"ema_trend":"UP" if e9>e21 else "DOWN"}

def classify_chart(bars):
 # bars are chronological completed OHLC candles (timestamp, low, high, open, close, volume).
 if len(bars)<25: return {"pattern":"INSUFFICIENT_DATA","bias":"WAIT"}
 highs=[float(b[2]) for b in bars]
 lows=[float(b[1]) for b in bars]
 closes=[float(b[4]) for b in bars]
 vols=[float(b[5]) for b in bars]
 last=closes[-1]
 recent_high=max(highs[-11:-1])
 recent_low=min(lows[-11:-1])
 previous_high=max(highs[-21:-11])
 previous_low=min(lows[-21:-11])
 range_pct=100*(recent_high-recent_low)/last
 vol_base=sum(vols[-21:-1])/20
 volume_spike=vol_base>0 and vols[-1]>1.5*vol_base
 bullish_structure=recent_high>previous_high and recent_low>previous_low
 bearish_structure=recent_high<previous_high and recent_low<previous_low
 if last>recent_high and volume_spike:
  pattern,bias="BULLISH_BREAKOUT","UP"
 elif last<recent_low and volume_spike:
  pattern,bias="BEARISH_BREAKOUT","DOWN"
 elif highs[-1]>recent_high and last<recent_high:
  pattern,bias="FAILED_UP_BREAKOUT","DOWN"
 elif lows[-1]<recent_low and last>recent_low:
  pattern,bias="FAILED_DOWN_BREAKOUT","UP"
 elif bullish_structure and closes[-1]<closes[-4] and closes[-4]>closes[-9]:
  pattern,bias="POSSIBLE_BEARISH_REVERSAL","DOWN"
 elif bearish_structure and closes[-1]>closes[-4] and closes[-4]<closes[-9]:
  pattern,bias="POSSIBLE_BULLISH_REVERSAL","UP"
 elif bullish_structure:
  pattern,bias="UPTREND","UP"
 elif bearish_structure:
  pattern,bias="DOWNTREND","DOWN"
 else:
  pattern,bias="RANGE","UP" if last>(recent_high+recent_low)/2 else "DOWN"
 # Describe current price behavior, not just directional bias.
 def change(n):
  return 100*(last/closes[-1-n]-1) if closes[-1-n] else 0
 move3=change(3)
 move5=change(5)
 move10=change(10)
 prior5=100*(closes[-6]/closes[-11]-1) if closes[-11] else 0
 acceleration=move5-prior5
 body=last-float(bars[-1][3])
 upper_wick=highs[-1]-max(last,float(bars[-1][3]))
 lower_wick=min(last,float(bars[-1][3]))-lows[-1]
 width=max(highs[-1]-lows[-1],0.000001)
 location=(last-recent_low)/max(recent_high-recent_low,0.000001)
 threshold=max(0.008,range_pct*0.15)
 if pattern in ("BULLISH_BREAKOUT","BEARISH_BREAKOUT"):
  behavior="BREAKOUT_WITH_VOLUME"
 elif pattern=="FAILED_UP_BREAKOUT":
  behavior="RESISTANCE_REJECTION"
 elif pattern=="FAILED_DOWN_BREAKOUT":
  behavior="SUPPORT_REJECTION"
 elif abs(move5)<threshold and range_pct<0.10:
  behavior="TIGHT_CONSOLIDATION"
 elif bullish_structure and move3< -threshold:
  behavior="PULLBACK_IN_UPTREND"
 elif bearish_structure and move3>threshold:
  behavior="BOUNCE_IN_DOWNTREND"
 elif abs(move5)>threshold and move5*acceleration>0 and abs(acceleration)>threshold:
  behavior="MOMENTUM_ACCELERATING"
 elif abs(move5)>threshold and move5*acceleration<0 and abs(acceleration)>threshold:
  behavior="MOMENTUM_FADING"
 elif abs(move5)<=threshold:
  behavior="SIDEWAYS_CHOP"
 else:
  behavior="TREND_CONTINUATION" if (bullish_structure and move5>0) or (bearish_structure and move5<0) else "MIXED_PRICE_ACTION"
 if upper_wick/width>0.55 and last<highs[-1] and behavior not in ("BREAKOUT_WITH_VOLUME","RESISTANCE_REJECTION"):
  behavior="UPPER_WICK_REJECTION"
 elif lower_wick/width>0.55 and behavior not in ("BREAKOUT_WITH_VOLUME","SUPPORT_REJECTION"):
  behavior="LOWER_WICK_REJECTION"
 return {"pattern":pattern,"bias":bias,"current_behavior":behavior,
  "structure":"HIGHER_HIGHS_HIGHER_LOWS" if bullish_structure else "LOWER_HIGHS_LOWER_LOWS" if bearish_structure else "MIXED_OR_RANGE",
  "momentum_3m_pct":round(move3,5),"momentum_5m_pct":round(move5,5),
  "momentum_10m_pct":round(move10,5),"prior_5m_pct":round(prior5,5),
  "momentum_acceleration_5m_pct":round(acceleration,5),
  "range_position_pct":round(100*location,1),
  "distance_to_support_pct":round(100*(last/recent_low-1),5) if recent_low else None,
  "distance_to_resistance_pct":round(100*(recent_high/last-1),5) if last else None,
  "last_candle_body_direction":"UP" if body>0 else "DOWN" if body<0 else "DOJI",
  "last_candle_upper_wick_ratio":round(upper_wick/width,3),
  "last_candle_lower_wick_ratio":round(lower_wick/width,3),
  "support":round(recent_low,2),"resistance":round(recent_high,2),
  "range_pct":round(range_pct,4),"volume_spike":volume_spike,
  "note":"Heuristic descriptions of completed 1m candles, not confirmed future direction"}

def graph_evidence(candles, volume_idx):
 """Compact chronological completed candles and price-structure measurements."""
 bars=[[int(float(x[0])),round(float(x[3]),2),round(float(x[2]),2),
        round(float(x[1]),2),round(float(x[4]),2),round(float(x[volume_idx]),5)]
       for x in candles[-30:]]
 closes=[b[4] for b in bars]
 opens=[b[1] for b in bars]
 highs=[b[2] for b in bars]
 lows=[b[3] for b in bars]
 def pct(a,b): return round(100*(a/b-1),5) if b else None
 last=closes[-1]
 return {"timeframe":"1m_completed","candles_ohlcv":bars,
  "candle_fields":["unix_time","open","high","low","close","volume"],
  "price_change_3m_pct":pct(last,closes[-4]),
  "price_change_10m_pct":pct(last,closes[-11]),
  "price_change_20m_pct":pct(last,closes[-21]),
  "last_5_higher_highs":sum(highs[i]>highs[i-1] for i in range(len(highs)-4,len(highs))),
  "last_5_higher_lows":sum(lows[i]>lows[i-1] for i in range(len(lows)-4,len(lows))),
  "last_5_bullish_candles":sum(closes[i]>opens[i] for i in range(-5,0)),
  "last_candle_body_pct":round(100*(last-opens[-1])/last,5),
  "last_upper_wick_pct":round(100*(highs[-1]-max(opens[-1],last))/last,5),
  "last_lower_wick_pct":round(100*(min(opens[-1],last)-lows[-1])/last,5),
  "recent_10m_high":max(highs[-10:]),"recent_10m_low":min(lows[-10:])}

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
  if len(candles)<35: raise ValueError("Coinbase insufficient completed candles")
  if time.time()-float(candles[-1][0])-60>90: raise ValueError("Coinbase latest completed candle stale")
  candles=candles[-40:]
  chart=chart_indicators(candles,5)
  pattern=classify_chart(candles)
  closes=[float(x[4]) for x in candles]
  vols=[float(x[5]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Coinbase BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-6]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence(candles,5),"source":"Coinbase Exchange BTC-USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
 except Exception as e: errors.append("Coinbase: "+str(e)[:150])
 # Kraken OHLC uses BTC/USD minute candles; last candle is incomplete.
 try:
  r=await client.get("https://api.kraken.com/0/public/OHLC",params={"pair":"XBTUSD","interval":1},timeout=12)
  r.raise_for_status()
  payload=r.json()
  if payload.get("error"): raise ValueError(str(payload["error"]))
  key=next(k for k in payload["result"] if k!="last")
  candles=sorted(payload["result"][key],key=lambda x:float(x[0]))[:-1]
  if len(candles)<35: raise ValueError("Kraken insufficient candles")
  if time.time()-float(candles[-1][0])-60>90: raise ValueError("Kraken latest completed candle stale")
  candles=candles[-40:]
  chart=chart_indicators(candles,6)
  pattern=classify_chart([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles])
  closes=[float(x[4]) for x in candles]
  vols=[float(x[6]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Kraken BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-6]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence(candles,6),"source":"Kraken BTC/USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
 except Exception as e: errors.append("Kraken: "+str(e)[:150])
 raise RuntimeError("External BTC feed unavailable: "+"; ".join(errors))
def judge(d):
 # Conservative multi-horizon confluence. Scores are evidence, not probabilities.
 fast=float(d["momentum_pct"])
 slow=float(d["long_momentum_pct"])
 volume=float(d["volume_ratio"])
 age=float(d.get("candle_age_seconds",999))
 if age>120 or volume<0.20:
  d["signal_quality"]={"status":"INSUFFICIENT_DATA","reason":"stale_or_thin_candles","confidence":"UNRATED"}
  return "WAIT","WAIT",0.0
 # Both 5-minute and 15-minute trends must agree for a directional call.
 aligned=(fast>0.045 and slow>0.075) or (fast< -0.045 and slow< -0.075)
 fast_vote=max(-1.5,min(1.5,fast*12))
 slow_vote=max(-1.0,min(1.0,slow*5))
 score=fast_vote+slow_vote
 if volume>=1.2: score*=1.10
 chart=d.get("chart") or {}
 if chart:
  trend=chart.get("ema_trend")
  rsi=float(chart.get("rsi14",50))
  score+=(0.35 if trend=="UP" else -0.35)+(0.25 if rsi>=55 else -0.25 if rsi<=45 else 0)
  if (fast>0 and trend=="DOWN") or (fast<0 and trend=="UP"): score*=0.65
 pattern=d.get("chart_pattern") or {}
 if pattern.get("bias") in ("UP","DOWN"):
  vote=0.35 if pattern["bias"]=="UP" else -0.35
  if pattern["pattern"]=="RANGE": vote*=0.3
  score+=vote
 # No fabricated buy/sell pressure: Coinbase candles provide no taker-side split.
 if d.get("pressure_available"):
  score+=max(-0.5,min(0.5,float(d.get("pressure",0))*1.5))
 direction=("UP" if score>0.95 else "DOWN" if score< -0.95 else "WAIT") if aligned else "WAIT"
 # A flip watch is a *watch*, not a confirmed reversal or calibrated probability.
 flip="WAIT"
 if slow< -0.09 and fast>0.035 and volume>=0.8:
  flip="POSSIBLE UP FLIP"
 elif slow>0.09 and fast< -0.035 and volume>=0.8:
  flip="POSSIBLE DOWN FLIP"
 d["signal_quality"]={"status":"CONFIRMED_TREND" if direction!="WAIT" else "MIXED_OR_WEAK","fast_5m_pct":fast,"slow_15m_pct":slow,"volume_ratio":volume,"trend_aligned":aligned,"confidence":"UNRATED","note":"Score is not a win probability; official Polymarket outcomes required for calibration"}
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
  for key in ("settlement","settlementPrice","settlement_price","price","value"):
   if key in obj and obj[key] is not None:
    try: return float(obj[key])
    except (TypeError,ValueError): pass
 return None

def verified_winner(m, settlement):
 # For official Polymarket US asset-price UP_DOWN contracts, YES pays on UP.
 # Reject any other market type or ambiguous settlement.
 if not isinstance(m,dict): return None
 terms=m.get("assetPriceTerms")
 if not isinstance(terms,dict) or terms.get("marketType")!="ASSET_PRICE_MARKET_TYPE_UP_DOWN": return None
 asset=terms.get("asset") or {}
 if not isinstance(asset,dict) or str(asset.get("symbol") or "").lower()!="btc": return None
 sides=m.get("marketSides") or []
 if not isinstance(sides,list) or len(sides)!=2: return None
 yes=[s for s in sides if isinstance(s,dict) and s.get("long") is True]
 no=[s for s in sides if isinstance(s,dict) and s.get("long") is False]
 if len(yes)!=1 or len(no)!=1: return None
 if str(yes[0].get("description") or "").strip().lower()!="yes": return None
 if str(no[0].get("description") or "").strip().lower()!="no": return None
 p=settlement_price(settlement)
 if p==1.0: return "UP"
 if p==0.0: return "DOWN"
 return None

async def settle(client):
 # Resolve the original event when the market-slug endpoint is unavailable.
 # Never infer official outcomes from Coinbase spot candles.
 from datetime import timedelta
 pending=execute("SELECT DISTINCT market FROM signals WHERE result='PENDING' ORDER BY market DESC",fetch=True)
 diagnostic={"pending_markets":len(pending),"checked":0,"verified":0,"unresolved":0,"errors":[],"reasons":{}}
 def unresolved(reason):
  diagnostic["unresolved"]+=1
  diagnostic["reasons"][reason]=diagnostic["reasons"].get(reason,0)+1
 for (slug,) in pending[:30]:
  try:
   # The US slug embeds the start time, e.g. cpc-btc-updown-5m-2026-10-25-0525z.
   stamp=slug.split("btc-updown-15m-" if "btc-updown-15m-" in slug else "btc-updown-5m-",1)[-1]
   start=datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
   if datetime.now(timezone.utc)<start+timedelta(minutes=16 if "btc-updown-15m-" in slug else 6):
    unresolved("round_not_yet_settled")
    continue
   diagnostic["checked"]+=1
   gateway="https://gateway.polymarket.us"
   event_slug=("btc-updown-15m-" if "btc-updown-15m-" in slug else "btc-updown-5m-")+stamp
   market=None
   # Event lookup is the same discovery path used to find the live contract.
   er=await client.get(gateway+"/v1/events/slug/"+event_slug,timeout=10)
   if er.status_code==200:
    ep=er.json()
    ev=ep.get("event",ep) if isinstance(ep,dict) else {}
    contracts=ev.get("markets",[]) if isinstance(ev,dict) else []
    if not contracts and isinstance(ep,dict): contracts=ep.get("markets",[])
    for candidate in contracts if isinstance(contracts,list) else []:
     if isinstance(candidate,dict) and str(candidate.get("slug") or candidate.get("marketSlug") or "")==slug:
      market=candidate
      break
   if market is None:
    for path in ("/v1/markets/"+slug,"/v1/market/slug/"+slug):
     mr=await client.get(gateway+path,timeout=10)
     if mr.status_code==200:
      mp=mr.json()
      candidate=mp.get("market",mp) if isinstance(mp,dict) else None
      if isinstance(candidate,dict) and str(candidate.get("slug") or candidate.get("marketSlug") or "")==slug:
       market=candidate
       break
   if market is None:
    unresolved("official_market_unavailable_event_"+str(er.status_code))
    continue
   status=str(market.get("status") or market.get("marketStatus") or "").upper()
   if "RESOLVED" not in status:
    unresolved("official_market_not_resolved_"+(status or "unknown")[:30])
    continue
   sr=await client.get(gateway+"/v1/markets/"+slug+"/settlement",timeout=10)
   if sr.status_code!=200:
    unresolved("settlement_not_published" if sr.status_code==404 else "official_settlement_http_"+str(sr.status_code))
    continue
   win=verified_winner(market,sr.json())
   if win is None:
    # Expose sanitized API schema so mapping failures can be fixed from real responses.
    examples=diagnostic.setdefault("mapping_samples",[])
    if len(examples)<3:
     sides=market.get("marketSides") or market.get("market_sides") or []
     examples.append({"market":slug,"market_keys":list(market.keys())[:30],
      "side_samples":[{k:(str(v)[:90] if isinstance(v,(str,int,float,bool)) else type(v).__name__) for k,v in side.items() if k in ("long","side","position","description","outcome","name","label","identifier","id","status")} for side in sides[:3] if isinstance(side,dict)] if isinstance(sides,list) else [],
      "settlement_type":type(sr.json()).__name__,
      "settlement_keys":list(sr.json().keys())[:20] if isinstance(sr.json(),dict) else [],
      "settlement_value":settlement_price(sr.json()),
      "question":str(market.get("question") or "")[:240],
      "title":str(market.get("title") or "")[:160],
      "subtitle":str(market.get("subtitle") or "")[:160],
      "outcomes":str(market.get("outcomes") or "")[:160],
      "asset_price_terms":str(market.get("assetPriceTerms") or "")[:350]})
    unresolved("official_winner_mapping_unverified")
    continue
   execute("UPDATE signals SET result=CASE WHEN direction='WAIT' THEN 'NO_CALL' WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING'",(win,slug))
   diagnostic["verified"]+=1
  except Exception as exc:
   diagnostic["errors"].append(type(exc).__name__+": "+str(exc)[:100])
 state["settlement_diagnostic"]=diagnostic
def final_direction(data, primary, score):
 # Research-only forced binary prediction, including weak/mixed conditions.
 # The decision is not a confidence estimate or guaranteed trading edge.
 if primary in ("UP","DOWN"): return primary
 fast=float(data.get("momentum_pct",0))
 slow=float(data.get("long_momentum_pct",0))
 combined=fast*0.7+slow*0.3
 if combined==0: combined=float(score)
 if combined==0:
  combined=float(data.get("last_price",0))-float(data.get("open_price",0))
 return "UP" if combined>=0 else "DOWN"

def signal_quality_snapshot(d, direction, score):
 """Snapshot evidence at call time, not after outcome is known."""
 q=d.get("signal_quality") or {}
 volume=float(d.get("volume_ratio") or 0)
 fast=float(d.get("momentum_pct") or 0)
 slow=float(d.get("long_momentum_pct") or 0)
 aligned=fast*slow>0 and abs(fast)>=0.025 and abs(slow)>=0.045
 freshness=float(d.get("candle_age_seconds") or 9999)
 if q.get("status")=="INSUFFICIENT_DATA" or freshness>100 or volume<0.6:
  return "LOW","Thin/stale candles or low volume"
 if direction not in ("UP","DOWN"):
  return "LOW","Forced binary call without primary confirmation"
 if aligned and volume>=1.0 and abs(float(score))>=2:
  return "HIGH","Aligned 5m/15m momentum, volume and score"
 if aligned and volume>=0.6 and abs(float(score))>=1:
  return "MEDIUM","Aligned momentum with moderate supporting evidence"
 return "LOW","Momentum disagreement or insufficient confluence"

def log_signal(slug,phase,direction,flip,score,now,d):
 quality,reason=signal_quality_snapshot(d,direction,score)
 execute("INSERT INTO signals (id,market,phase,direction,flip,score,created_at,quality,quality_reason) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(slug+":"+phase,slug,phase,direction,flip,score,now,quality,reason))

def accuracy_report():
 rows=execute("SELECT phase,quality,result,COUNT(*) FROM signals GROUP BY phase,quality,result",fetch=True)
 report={}
 for phase,quality,result,n in rows:
  key=phase+" / "+(quality or "UNRATED")
  item=report.setdefault(key,{"phase":phase,"quality":quality or "UNRATED","WIN":0,"LOSS":0,"PENDING":0,"NO_CALL":0})
  if result in item: item[result]+=n
 for item in report.values():
  graded=item["WIN"]+item["LOSS"]
  item["graded"]=graded
  item["accuracy_pct"]=round(100*item["WIN"]/graded,1) if graded else None
 return sorted(report.values(),key=lambda x:(x["phase"],x["quality"]))

async def record_halfway(client,slug,phase,d,primary,flip,score,now,market):
 existing=execute("SELECT id FROM signals WHERE id=?",(slug+":"+phase,),fetch=True)
 if existing: return
 fallback=final_direction(d,primary,score)
 terms=market.get("assetPriceTerms") if isinstance(market,dict) else None
 ptb=(terms or {}).get("priceToBeat") if isinstance(terms,dict) else None
 target=ptb.get("value") if isinstance(ptb,dict) else None
 review=await halfway_decision(client,slug,15 if phase=="FINAL_15M" else 5,fallback,score,d,target)
 chosen=review.get("direction") if review.get("direction") in ("UP","DOWN") else fallback
 log_signal(slug,phase,chosen,flip,score,now,d)
 execute("UPDATE signals SET decision_source=?,decision_reason=?,ai_confidence=? WHERE id=?",
  (review.get("source","UNKNOWN"),str(review.get("reason",""))[:300],review.get("ai_confidence","UNRATED"),slug+":"+phase))
 state["last_halfway_ai"]={"market":slug,"phase":phase,"direction":chosen,
  "indicator_direction":fallback,"source":review.get("source"),
  "reason":review.get("reason"),"ai_confidence":review.get("ai_confidence"),
  "evidence":review.get("evidence"),
  "updated":now}

async def loop():
 setup()
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
  while True:
   cycle_started=time.monotonic()
   try:
    await check_auth(client)
    m=await market_data(client)
    try:
     d=await spot_data(client)
    except Exception as feed_exc:
     d={"source":"unavailable","candle_age_seconds":None,"signal_quality":{"status":"INSUFFICIENT_DATA","reason":"No fresh verified BTC candles"},"feed_error":str(feed_exc)[:300]}
    direction,flip,score=judge(d) if d.get("price") is not None else ("WAIT","WAIT",0.0)
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
      log_signal(slug,phase,direction,flip,score,now,d)
     # One independent, immutable FINAL call per 5m round, at the first poll
     # reaching 2:30 remaining. Force a research prediction even when trend confirmation is weak.
     if remaining<=150 and remaining>0 and d.get("price") is not None:
      await record_halfway(client,slug,"FINAL",d,direction,flip,score,now,m)
     state.update(status="running",signal=direction,flip=flip,score=score,phase=phase,seconds_left=round(remaining),market=slug,external=d,updated=now,error=None)
     await confirm(client,slug,phase,direction,flip,score,d)
    # Dedicated 15m FINAL call at 7:30 remaining, independent of 5m FINAL.
    fifteen,reason15=await market_15m(client)
    if fifteen:
     left15=fifteen["end"]-time.time()
     state["final_15m"]={"market":fifteen["slug"],"seconds_left":round(left15),"trigger_seconds":450,"lookup":"verified_contract"}
     if 0<left15<=450 and d.get("price") is not None:
      await record_halfway(client,fifteen["slug"],"FINAL_15M",d,direction,flip,score,now,fifteen)
    else:
     state["final_15m"]={"market":None,"lookup":reason15,"trigger_seconds":450}
    state["feed_health"]={"status":"FRESH" if d.get("price") is not None else "UNAVAILABLE","source":d.get("source"),"age_seconds":d.get("candle_age_seconds"),"error":d.get("feed_error")}
    await settle(client)
   except Exception as e:
    for key in ("phase","seconds_left","score"): state.pop(key,None)
    state.update(status="feed error",signal="WAIT",flip="WAIT",error=str(e),updated=datetime.now(timezone.utc).isoformat())
   await asyncio.sleep(max(0, POLL - (time.monotonic() - cycle_started)))
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
@app.get("/api/candles")
async def candle_proxy():
 """Same-origin 1m BTC candles for Safari when exchange CORS is blocked."""
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"},timeout=10) as client:
  errors=[]
  try:
   r=await client.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",params={"granularity":60})
   r.raise_for_status()
   raw=r.json()
   bars=[[float(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in raw if isinstance(x,list) and len(x)>=6]
   if bars:return {"source":"Coinbase","candles":bars}
  except Exception as e: errors.append("Coinbase: "+str(e)[:120])
  try:
   r=await client.get("https://api.kraken.com/0/public/OHLC",params={"pair":"XBTUSD","interval":1})
   r.raise_for_status()
   raw=r.json()
   if raw.get("error"):raise ValueError(str(raw["error"]))
   key=next(k for k in raw["result"] if k!="last")
   bars=[[float(x[0]),float(x[3]),float(x[2]),float(x[1]),float(x[4]),float(x[6])] for x in raw["result"][key]]
   if bars:return {"source":"Kraken","candles":bars}
  except Exception as e:errors.append("Kraken: "+str(e)[:120])
  return {"source":"unavailable","candles":[],"error":"; ".join(errors)}

@app.get("/api/status")
def status():
 try:
  rows=execute("SELECT result,COUNT(*) FROM signals GROUP BY result",fetch=True)
  recent=execute("SELECT market,phase,direction,flip,score,created_at,result,quality,quality_reason,decision_source,decision_reason,ai_confidence FROM signals ORDER BY created_at DESC LIMIT 50",fetch=True)
  return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"counts":dict(rows),"accuracy_by_quality":accuracy_report(),"history":[dict(zip(("market","phase","direction","flip","score","created_at","result","quality","quality_reason","decision_source","decision_reason","ai_confidence"),r)) for r in recent]}
 except Exception as e: return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"database_error":str(e),"history":[]}
@app.get("/",response_class=HTMLResponse)
def home():
 return Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")
