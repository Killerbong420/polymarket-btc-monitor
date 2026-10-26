import asyncio, json, os, sqlite3, time
from pathlib import Path
from historical_15m import main as collect_15m, START as HISTORY_START, END as HISTORY_END
from ai_confirmation import confirm, public_state, check_auth, auth_state, halfway_decision, paper_signal_state
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import httpx
from market_intelligence import intelligence_loop, public_intelligence
from research import VERSION, CHECKPOINTS, checkpoint_plan, reference_prices, target_behavior, market_quote, entry_cost, summarize, number, market_start, confluence_forecast
from price_action import analyze_price_action, shadow_price_action
from bot_library import study_strategies, consensus
from monitor_health import checks as health_checks, persist_alerts, alert_history
from fastapi import FastAPI, Query, Request
import paper_trader as paper
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
try:
 import psycopg
except ImportError:
 psycopg = None

# Fixed requested cadence also overrides legacy Render POLL_SECONDS=15 settings.
POLL = 5
DB = os.getenv("DATABASE_URL", "")
state = {"status":"starting","signal":"WAIT","flip":"WAIT","market":None,"updated":None,"error":None}
@contextmanager
def connect():
 c=psycopg.connect(DB) if DB else sqlite3.connect("monitor.db",timeout=10)
 try:
  yield c
  c.commit()
 except BaseException:
  c.rollback()
  raise
 finally:c.close()
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
 execute("""CREATE TABLE IF NOT EXISTS ai_decisions (
 id TEXT PRIMARY KEY, market TEXT NOT NULL, phase TEXT NOT NULL,
 direction TEXT NOT NULL, confidence TEXT NOT NULL, source TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL,
 result TEXT NOT NULL DEFAULT 'PENDING')""")
 for col in ("quality TEXT NOT NULL DEFAULT 'UNRATED'","quality_reason TEXT NOT NULL DEFAULT 'Legacy prediction; quality not captured'","decision_source TEXT NOT NULL DEFAULT 'LEGACY'","decision_reason TEXT NOT NULL DEFAULT ''","ai_confidence TEXT NOT NULL DEFAULT 'UNRATED'"):
  try: execute("ALTER TABLE signals ADD COLUMN "+col)
  except Exception as exc:
   if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower(): raise
 execute("""CREATE TABLE IF NOT EXISTS research_calls (
 id TEXT PRIMARY KEY,market TEXT NOT NULL,checkpoint TEXT NOT NULL,created_at TEXT NOT NULL,
 snapshot_json TEXT NOT NULL,calls_json TEXT NOT NULL,winner TEXT,settled_at TEXT)""")
 execute("""CREATE TABLE IF NOT EXISTS monitor_alerts (
 code TEXT PRIMARY KEY,severity TEXT NOT NULL,message TEXT NOT NULL,active INTEGER NOT NULL,
 first_seen TEXT NOT NULL,last_changed TEXT NOT NULL)""")
 execute("""CREATE TABLE IF NOT EXISTS monitor_events (
 id TEXT PRIMARY KEY,code TEXT NOT NULL,severity TEXT NOT NULL,message TEXT NOT NULL,
 active INTEGER NOT NULL,created_at TEXT NOT NULL)""")
 execute("CREATE INDEX IF NOT EXISTS research_market_idx ON research_calls(market)")
 paper.setup(execute)
 execute("""CREATE TABLE IF NOT EXISTS polymarket_quote_samples (
   id TEXT PRIMARY KEY, market TEXT NOT NULL,sampled_epoch REAL NOT NULL,
   up_bid REAL,up_ask REAL,down_bid REAL,down_ask REAL,spread REAL)""")
 execute("CREATE INDEX IF NOT EXISTS pm_quote_market_epoch ON polymarket_quote_samples (market,sampled_epoch)")
_last_quote_cleanup=0.0
def capture_polymarket_quote(quote):
  """Store real Polymarket 15-minute UP/DOWN book quotes, not BTC spot data."""
  global _last_quote_cleanup
  if quote.get("status")!="available" or "btc-updown-15m-" not in str(quote.get("market","")):return
  now=time.time()
  if now-float(quote.get("fetched_epoch") or 0)>15:return
  market=quote["market"]
  valid=lambda x:x is None or (isinstance(x,(int,float)) and 0<=x<=1)
  up_bid,up_ask=quote.get("bid"),quote.get("ask")
  if not valid(up_bid) or not valid(up_ask) or (up_bid is None and up_ask is None):return
  down_bid=round(1-up_ask,6) if up_ask is not None else None
  down_ask=round(1-up_bid,6) if up_bid is not None else None
  key=market+":"+str(int(now//5))
  execute("""INSERT INTO polymarket_quote_samples
   (id,market,sampled_epoch,up_bid,up_ask,down_bid,down_ask,spread)
   VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
   sampled_epoch=excluded.sampled_epoch,up_bid=excluded.up_bid,up_ask=excluded.up_ask,
   down_bid=excluded.down_bid,down_ask=excluded.down_ask,spread=excluded.spread""",
   (key,market,now,up_bid,up_ask,down_bid,down_ask,quote.get("spread")))
  paper.observe_quote_opportunities(execute,quote)
  if now-_last_quote_cleanup>3600:
   execute("DELETE FROM polymarket_quote_samples WHERE sampled_epoch<?",(now-172800,))
   _last_quote_cleanup=now

def parse_json(v):
 if isinstance(v,list): return v
 try: return json.loads(v)
 except (ValueError,TypeError): return []
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
 move_short=change(3)
 move10=change(10)
 prior_short=100*(closes[-4]/closes[-7]-1) if closes[-7] else 0
 acceleration=move_short-prior_short
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
 elif abs(move_short)<threshold and range_pct<0.10:
  behavior="TIGHT_CONSOLIDATION"
 elif bullish_structure and move3< -threshold:
  behavior="PULLBACK_IN_UPTREND"
 elif bearish_structure and move3>threshold:
  behavior="BOUNCE_IN_DOWNTREND"
 elif abs(move_short)>threshold and move_short*acceleration>0 and abs(acceleration)>threshold:
  behavior="MOMENTUM_ACCELERATING"
 elif abs(move_short)>threshold and move_short*acceleration<0 and abs(acceleration)>threshold:
  behavior="MOMENTUM_FADING"
 elif abs(move_short)<=threshold:
  behavior="SIDEWAYS_CHOP"
 else:
  behavior="TREND_CONTINUATION" if (bullish_structure and move_short>0) or (bearish_structure and move_short<0) else "MIXED_PRICE_ACTION"
 if upper_wick/width>0.55 and last<highs[-1] and behavior not in ("BREAKOUT_WITH_VOLUME","RESISTANCE_REJECTION"):
  behavior="UPPER_WICK_REJECTION"
 elif lower_wick/width>0.55 and behavior not in ("BREAKOUT_WITH_VOLUME","SUPPORT_REJECTION"):
  behavior="LOWER_WICK_REJECTION"
 return {"pattern":pattern,"bias":bias,"current_behavior":behavior,
  "structure":"HIGHER_HIGHS_HIGHER_LOWS" if bullish_structure else "LOWER_HIGHS_LOWER_LOWS" if bearish_structure else "MIXED_OR_RANGE",
  "momentum_3m_pct":round(move3,5),
  "momentum_10m_pct":round(move10,5),"prior_3m_pct":round(prior_short,5),
  "momentum_acceleration_3m_pct":round(acceleration,5),
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
  candles=candles[-80:]
  chart=chart_indicators(candles,5)
  pattern=classify_chart(candles)
  closes=[float(x[4]) for x in candles]
  vols=[float(x[5]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Coinbase BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-4]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence(candles,5),"price_action":analyze_price_action(candles,"Coinbase Exchange BTC-USD"),"bot_research_signals":study_strategies(candles,"Coinbase Exchange BTC-USD"),"source":"Coinbase Exchange BTC-USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
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
  candles=candles[-80:]
  chart=chart_indicators([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],5)
  pattern=classify_chart([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles])
  closes=[float(x[4]) for x in candles]
  vols=[float(x[6]) for x in candles]
  recent=sum(vols[-5:])
  baseline=sum(vols[-20:-5])/3
  if recent<0.05 or baseline<0.05: raise ValueError("Kraken BTC volume too thin")
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-4]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],5),"price_action":analyze_price_action([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],"Kraken BTC/USD"),"bot_research_signals":study_strategies([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],"Kraken BTC/USD"),"source":"Kraken BTC/USD","candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
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
 # Both 3-minute and 15-minute trends must agree for a directional call.
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
 d["signal_quality"]={"status":"CONFIRMED_TREND" if direction!="WAIT" else "MIXED_OR_WEAK","fast_3m_pct":fast,"slow_15m_pct":slow,"volume_ratio":volume,"trend_aligned":aligned,"confidence":"UNRATED","note":"Score is not a win probability; official Polymarket outcomes required for calibration"}
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
 pending=execute("SELECT market FROM signals WHERE result='PENDING' AND phase='FINAL_15M' AND market LIKE ? UNION SELECT market FROM ai_decisions WHERE result='PENDING' AND phase='FINAL_15M' AND source='AI_REVIEW' UNION SELECT market FROM research_calls WHERE winner IS NULL UNION SELECT market FROM paper_positions WHERE status='OPEN' ORDER BY market DESC",("%btc-updown-15m-%",),fetch=True)
 diagnostic={"pending_markets":len(pending),"checked":0,"verified":0,"unresolved":0,"errors":[],"reasons":{}}
 def unresolved(reason):
  diagnostic["unresolved"]+=1
  diagnostic["reasons"][reason]=diagnostic["reasons"].get(reason,0)+1
 for (slug,) in pending[:30]:
  try:
   # The verified 15-minute contract slug embeds its UTC start time.
   stamp=slug.split("btc-updown-15m-",1)[-1]
   start=datetime.strptime(stamp,"%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
   if datetime.now(timezone.utc)<start+timedelta(minutes=16):
    unresolved("round_not_yet_settled")
    continue
   diagnostic["checked"]+=1
   gateway="https://gateway.polymarket.us"
   event_slug="btc-updown-15m-"+stamp
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
   if "RESOLVED" not in status or "UNRESOLVED" in status:
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
   execute("UPDATE ai_decisions SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING'",(win,slug))
   execute("UPDATE research_calls SET winner=?,settled_at=? WHERE market=? AND winner IS NULL",(win,datetime.now(timezone.utc).isoformat(),slug))
   execute("UPDATE paper_positions SET winner=? WHERE market=? AND status='OPEN'",(win,slug))
   diagnostic["verified"]+=1
  except Exception as exc:
   diagnostic["errors"].append(type(exc).__name__+": "+str(exc)[:100])
 diagnostic["overdue_markets"]=sum(1 for (slug,) in pending if market_start(slug) is not None and time.time()>market_start(slug)+2700)
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
 freshness=float(d.get("candle_age_seconds") if d.get("candle_age_seconds") is not None else 9999)
 if q.get("status")=="INSUFFICIENT_DATA" or freshness>100 or volume<0.6:
  return "LOW","Thin/stale candles or low volume"
 if direction not in ("UP","DOWN"):
  return "LOW","Forced binary call without primary confirmation"
 if aligned and volume>=1.0 and abs(float(score))>=2:
  return "HIGH","Aligned 3m/15m momentum, volume and score"
 if aligned and volume>=0.6 and abs(float(score))>=1:
  return "MEDIUM","Aligned momentum with moderate supporting evidence"
 return "LOW","Momentum disagreement or insufficient confluence"

def log_signal(slug,phase,direction,flip,score,now,d):
 quality,reason=signal_quality_snapshot(d,direction,score)
 execute("INSERT INTO signals (id,market,phase,direction,flip,score,created_at,quality,quality_reason) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(slug+":"+phase,slug,phase,direction,flip,score,now,quality,reason))

def accuracy_report():
 rows=execute("SELECT phase,quality,result,COUNT(*) FROM signals WHERE phase='FINAL_15M' GROUP BY phase,quality,result",fetch=True)
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

def research_rows():
 return execute("SELECT market,checkpoint,created_at,snapshot_json,calls_json,winner FROM research_calls ORDER BY created_at DESC LIMIT 10000",fetch=True)

_research_cache={"updated_epoch":0,"report":None}
def research_report():
 if time.time()-_research_cache["updated_epoch"]>30 or _research_cache["report"] is None:
  report=summarize(research_rows())
  legacy=execute("SELECT confidence,result,COUNT(*) FROM ai_decisions WHERE source='AI_REVIEW' AND phase='FINAL_15M' GROUP BY confidence,result",fetch=True)
  report["previous_halfway_ai_confidence"]=[{"confidence":c,"result":r,"count":n} for c,r,n in legacy]
  report["max_checkpoints_in_report"]=10000
  _research_cache.update(updated_epoch=time.time(),report=report)
 return {**_research_cache["report"],"updated_epoch":_research_cache["updated_epoch"]}

def prediction_loss_review(rows):
 """Bounded, same-checkpoint diagnostics from settled forward calls only."""
 groups={};recent=[]
 for market,checkpoint,created,snapshot_json,calls_json,winner in rows:
  if winner not in ('UP','DOWN'):continue
  try:
   snapshot=json.loads(snapshot_json);calls=json.loads(calls_json)
  except (TypeError,ValueError):continue
  if not isinstance(snapshot,dict) or not isinstance(calls,dict):continue
  ai=calls.get('AI') or {}
  if not isinstance(ai,dict) or ai.get('source')!='AI_REVIEW' or ai.get('direction') not in ('UP','DOWN'):continue
  behavior=snapshot.get('target_behavior') or {}
  if not isinstance(behavior,dict):behavior={}
  version=snapshot.get('version','UNKNOWN')
  tags=['ALL_AI_CALLS']
  if behavior.get('status')=='available':
   side=behavior.get('side')
   if side in ('UP','DOWN'):tags.append('WITH_TARGET_SIDE' if side==ai['direction'] else 'AGAINST_TARGET_SIDE')
   if behavior.get('near_target') is True:tags.append('NEAR_TARGET')
   crossings=number(behavior.get('observed_crossings'))
   if crossings is not None and crossings>=3:tags.append('REPEATED_TARGET_CROSSINGS')
   if behavior.get('lead_behavior')=='SHRINKING':tags.append('SHRINKING_LEAD')
  correct=ai['direction']==winner
  for tag in tags:
   key=(checkpoint,version,tag)
   group=groups.setdefault(key,{'checkpoint':checkpoint,'version':version,'condition':tag,'wins':0,'losses':0})
   group['wins' if correct else 'losses']+=1
  if not correct and len(recent)<12:
   recent.append({'market':market,'checkpoint':checkpoint,'version':version,'captured_at':created,
    'predicted':ai['direction'],'winner':winner,'confidence':ai.get('confidence'),
    'conditions':tags,'target_gap_pct':behavior.get('target_gap_pct'),
    'is_reference':behavior.get('is_reference'),'coverage_pct':behavior.get('coverage_pct'),
    'reason':str(ai.get('reason') or '')[:500]})
 cohorts=[]
 for group in groups.values():
  n=group['wins']+group['losses']
  group.update(graded=n,accuracy_pct=round(100*group['wins']/n,1),sample_label='SMALL SAMPLE' if n<100 else 'OBSERVED SAMPLE')
  cohorts.append(group)
 return {'cohorts':cohorts,'recent_losses':recent,
  'interpretation':'Conditions overlap; compare only within the same checkpoint and version. Loss examples are hindsight audits, not causal rules or calibrated probabilities. Missing target evidence is not classified. Validate any proposed rule on subsequent unseen rounds; do not override a forecast solely to fit these losses.'}

_outcome_feedback_cache={}
def outcome_feedback(market):
 cached=_outcome_feedback_cache.get(market)
 if cached and time.time()-cached[0]<60:return cached[1]
 rows=[row for row in research_rows() if row[0]!=market and row[5] in ('UP','DOWN')]
 report=summarize(rows)
 feedback={"source":"settled forward prediction log","excluded_market":market,
  "strategies":report.get("strategies",[]),"confidence":report.get("confidence",[]),
  "paired_comparisons":report.get("paired_comparisons",[]),
  "loss_review":prediction_loss_review(rows),
  "limitations":"Small observational samples across multiple code versions. Repeated checkpoints share rounds. Confidence labels are not calibrated probabilities. Do not automatically select the highest historical accuracy or infer trading profitability. Official published target overrides reconstructed values."}
 _outcome_feedback_cache.clear();_outcome_feedback_cache[market]=(time.time(),feedback)
 return feedback

async def capture_checkpoint(client,slug,checkpoint,left,d,primary,flip,score,market):
 key=slug+":"+checkpoint
 if execute("SELECT id FROM research_calls WHERE id=?",(key,),fetch=True):return
 # Freeze common evidence BEFORE the AI call; later price changes cannot rewrite it.
 import copy
 frozen=copy.deepcopy(d);captured=datetime.now(timezone.utc).isoformat()
 target=number(((market.get("assetPriceTerms") or {}).get("priceToBeat")))
 behavior=frozen.get("target_behavior") or {}
 quote=frozen.get("market_quote") or {}
 fallback=final_direction(frozen,primary,score)
 review=await halfway_decision(client,slug,15,fallback,score,frozen,target,seconds_left=left,checkpoint=checkpoint)
 chosen=review.get("direction") if review.get("direction") in ("UP","DOWN") else fallback
 confidence=review.get("ai_confidence","UNRATED")
 quality,quality_reason=signal_quality_snapshot(frozen,primary,score)
 calls={
  "AI":{"direction":chosen if review.get("source")=="AI_REVIEW" else "WAIT","source":review.get("source"),"confidence":confidence,"reason":review.get("reason"),"news_assessment":review.get("news_assessment")},
  "INDICATORS":{"direction":primary,"source":"INDICATORS","confidence":quality,"reason":quality_reason},
  "INDICATOR_BINARY":{"direction":fallback,"source":"FORCED_INDICATORS","confidence":quality},
  "TARGET_SIDE":{"direction":behavior.get("side") if behavior.get("status")=="available" and behavior.get("side") in ("UP","DOWN") else "WAIT","source":behavior.get("source"),"confidence":"UNRATED"}}
 # Shadow strategy: log abstentions as well as calls, on the same frozen forward checkpoint.
 # Never overwrite the raw AI outcome, main 15m lock, or trading controls.
 calls["CONFLUENCE"]=confluence_forecast(calls["AI"],behavior,frozen.get("reference_price"),frozen.get("chart"))
 calls["PRICE_ACTION"]=shadow_price_action(frozen.get("price_action"),frozen.get("candle_age_seconds"))
 for strategy,call in (frozen.get("bot_research_signals") or {}).items():
  if strategy in ("BOT_MACD","BOT_BOLLINGER","BOT_SUPERTREND"):
   calls[strategy]=dict(call) if frozen.get("candle_age_seconds") is not None and frozen["candle_age_seconds"]<=75 else {"direction":"WAIT","source":"BOT_STALE","confidence":"UNRATED"}
 calls["BOT_CONSENSUS"]=consensus(calls)
 for call in calls.values():
  direction=call.get("direction")
  px=quote.get("up_entry" if direction=="UP" else "down_entry") if direction in ("UP","DOWN") and quote.get("status")=="available" else None
  call["entry_cost"]=entry_cost(px)
 snapshot={"version":VERSION,"captured_at":captured,"seconds_left":left,"checkpoint":checkpoint,
  "official_target":target,"target_behavior":behavior,"reference_price":frozen.get("reference_price"),
  "market_quote":quote,"chart_pattern":frozen.get("chart_pattern"),"price_action":frozen.get("price_action"),"bot_research_signals":frozen.get("bot_research_signals"),"graph_evidence":frozen.get("graph_evidence"),
  "external_intelligence":frozen.get("external_intelligence"),"news_assessment":review.get("news_assessment"),
  "outcome_feedback":frozen.get("outcome_feedback"),"indicator_score":score,"ai_model":review.get("model"),"historical_context":(review.get("evidence") or {}).get("historical_context")}
 # The checkpoint and its legacy halfway rows commit together or roll back together.
 with connect() as c:
  cur=c.cursor()
  def run(sql,args):cur.execute(sql.replace("?","%s") if DB else sql,args)
  try:
   run("INSERT INTO research_calls (id,market,checkpoint,created_at,snapshot_json,calls_json) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(key,slug,checkpoint,captured,json.dumps(snapshot),json.dumps(calls)))
   if cur.rowcount and checkpoint=="HALFWAY":
    ident=slug+":FINAL_15M"
    run("INSERT INTO signals (id,market,phase,direction,flip,score,created_at,quality,quality_reason,decision_source,decision_reason,ai_confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(ident,slug,"FINAL_15M",chosen,flip,score,captured,quality,quality_reason,review.get("source","INDICATORS_FALLBACK"),str(review.get("reason",""))[:300],confidence))
    run("INSERT INTO ai_decisions (id,market,phase,direction,confidence,source,reason,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",(ident,slug,"FINAL_15M",chosen,confidence,review.get("source","INDICATORS_FALLBACK"),str(review.get("reason",""))[:300],captured))
  finally:cur.close()
 if checkpoint=="HALFWAY":state["last_halfway_ai"]={"market":slug,"phase":"FINAL_15M","direction":chosen,"indicator_direction":fallback,"source":review.get("source"),"reason":review.get("reason"),"ai_confidence":confidence,"updated":captured,"news_assessment":review.get("news_assessment")}
 _research_cache["updated_epoch"]=0
 state["last_checkpoint"]={"market":slug,"checkpoint":checkpoint,"captured_at":captured,"seconds_left":left,"calls":calls}

_prediction_tasks=set()
def track_prediction(task):
 _prediction_tasks.add(task)
 def finished(t):
  _prediction_tasks.discard(t)
  if not t.cancelled():t.exception()
 task.add_done_callback(finished)
 return task

async def loop():
 setup()
 ai_task=None
 checkpoints={}
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
  while True:
   cycle_started=time.monotonic()
   try:
    state["loop_started_epoch"]=time.time()
    auth_result,spot_result,market_result=await asyncio.gather(
     check_auth(client),spot_data(client),market_15m(client),return_exceptions=True)
    if isinstance(spot_result,BaseException):
     d={"source":"unavailable","candle_age_seconds":None,"signal_quality":{"status":"INSUFFICIENT_DATA","reason":"No fresh verified BTC candles"},"feed_error":str(spot_result)[:300]}
    else:d=spot_result
    if isinstance(market_result,BaseException):
     fifteen,reason15=None,"lookup_"+type(market_result).__name__
    else:fifteen,reason15=market_result
    state["market_lookup_reason"]=reason15
    state["ai_auth_error"]=type(auth_result).__name__ if isinstance(auth_result,BaseException) else None
    intel=public_intelligence()
    d["external_intelligence"]=intel
    fresh_flow=[x for x in intel["trade_flow"].get("exchanges",[]) if x.get("status")=="fresh"] if intel["trade_flow"].get("status")!="stale" else []
    buy=sum(x.get("buy_usd",0) for x in fresh_flow)
    sell=sum(x.get("sell_usd",0) for x in fresh_flow)
    d["pressure_available"]=buy+sell>0
    d["pressure"]=(buy-sell)/(buy+sell) if buy+sell>0 else 0.0
    d["pressure_source"]=" + ".join(x["source"] for x in fresh_flow) or "unavailable"
    direction,flip,score=judge(d) if d.get("price") is not None else ("WAIT","WAIT",0.0)
    now=datetime.now(timezone.utc).isoformat()
    if fifteen:
     slug=fifteen["slug"]
     left15=fifteen["end"]-time.time()
     phase="EARLY" if left15>450 else "LATE" if left15<=180 else "MID"
     state.update(status="running",signal=direction,flip=flip,score=score,phase=phase,seconds_left=round(left15),market=slug,external=d,updated=now,error=None)
     state["final_15m"]={"market":slug,"seconds_left":round(left15),"trigger_seconds":450,"lookup":"verified_contract"}
     try:d["outcome_feedback"]=outcome_feedback(slug)
     except Exception:d["outcome_feedback"]={"status":"unavailable"}
     terms=fifteen.get("assetPriceTerms") or {}
     ptb=terms.get("priceToBeat") or {}
     target=ptb.get("value") if isinstance(ptb,dict) else None
     reference,quote=await asyncio.gather(reference_prices(client,fifteen),market_quote(client,slug))
     # Display-only BRTI reference series from the Polymarket US reference API.
     # Keep raw index ticks in process memory; no CF Benchmarks site scraping.
     ref_points=reference.get("points") or []
     if reference.get("status") in ("fresh","stale") and ref_points:
      state["brti_chart"]={"market":slug,"source":reference.get("source"),
       "points":[[int(t),round(float(p),2)] for t,p in ref_points if t>=time.time()-16*60][::3],
       "sample_age_seconds":reference.get("age_seconds"),
       "observed_epoch":time.time()}
     else:state["brti_chart"]={"market":slug,"source":"Polymarket US reference API",
       "points":[],"status":"unavailable","reason":reference.get("reason","No verified BRTI observations")}
     ref_public={k:v for k,v in reference.items() if k!="points"}
     behavior=target_behavior(d,target,slug,reference)
     d.update(reference_price=ref_public,target_behavior=behavior,market_quote=quote)
     state.update(reference_price=ref_public,target_behavior=behavior,market_quote=quote)
     try:
      capture_polymarket_quote(quote)
      state["quote_storage_error"]=None
     except Exception as quote_exc:
      state["quote_storage_error"]=type(quote_exc).__name__+": "+str(quote_exc)[:150]
     avg=reference.get("opening_average");pt=number(target)
     state["target_validation"]={"status":"match" if abs(avg-pt)<=.02 else "mismatch","difference_usd":round(avg-pt,4)} if avg is not None and pt is not None else {"status":"unavailable"}
     captured={r[0] for r in execute("SELECT checkpoint FROM research_calls WHERE market=?",(slug,),fetch=True)}
     plan=checkpoint_plan(left15,captured)
     state["missed_checkpoints"]=plan["missed"]
     for name in plan["due"]:
      if d.get("price") is not None:
       key=(slug,name)
       if key not in checkpoints or checkpoints[key].done():
        checkpoints[key]=track_prediction(asyncio.create_task(capture_checkpoint(client,slug,name,round(left15),json.loads(json.dumps(d)),direction,flip,score,fifteen)))
     checkpoints={k:v for k,v in checkpoints.items() if not v.done()}
     d["paper_position"]=paper.position_context(execute,slug)
     if ai_task is None or ai_task.done():
      ai_task=asyncio.create_task(confirm(client,slug,phase,direction,flip,score,json.loads(json.dumps(d)),price_to_beat=target,seconds_left=round(left15)))
      track_prediction(ai_task)
    else:
     for key in ("phase","seconds_left","score"): state.pop(key,None)
     state.update(status="market unavailable",signal="WAIT",flip=flip,market=None,external=d,updated=now,error="No verified Polymarket US 15-minute market: "+str(reason15))
     state["final_15m"]={"market":None,"lookup":reason15,"trigger_seconds":450}
     state.update(reference_price={"status":"unavailable"},target_behavior={"status":"unavailable"},market_quote={"status":"unavailable"},missed_checkpoints=[])
     await confirm(client,None,None,"WAIT","WAIT",0,d)
    state["feed_health"]={"status":"FRESH" if d.get("price") is not None else "UNAVAILABLE","source":d.get("source"),"age_seconds":d.get("candle_age_seconds"),"error":d.get("feed_error")}
    await settle(client)
    state["alerts"]=health_checks(state,public_state(),DB)
    persist_alerts(execute,state["alerts"])
   except Exception as e:
    for key in ("phase","seconds_left","score"): state.pop(key,None)
    state.update(status="feed error",signal="WAIT",flip="WAIT",error=type(e).__name__+": "+str(e)[:200],updated=datetime.now(timezone.utc).isoformat())
    db_error=None
    try:execute("SELECT 1",fetch=True)
    except Exception:db_error="Database query failed"
    state["alerts"]=health_checks(state,public_state(),DB,db_error=db_error)
   state["last_cycle_duration_seconds"]=round(time.monotonic()-cycle_started,2)
   state["updated"]=datetime.now(timezone.utc).isoformat()
   try:await asyncio.sleep(max(0, POLL - (time.monotonic() - cycle_started)))
   except asyncio.CancelledError:
    if ai_task is not None:
     ai_task.cancel()
    raise
async def paper_worker():
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchPaperSandbox/1.0"}) as client:
  while True:
   started=time.monotonic()
   try:
    await paper.cycle(client,execute,connect,bool(DB),{**paper_signal_state(),"live_quote":state.get("market_quote")})
    state["paper_error"]=None
    state["paper_unfilled_exits"]=execute("SELECT COUNT(*) FROM paper_orders WHERE status='UNFILLED'",fetch=True)[0][0]
   except Exception as exc:state["paper_error"]=type(exc).__name__+": "+str(exc)[:160]
   await asyncio.sleep(max(0,1-(time.monotonic()-started)))

@asynccontextmanager
async def lifespan(app):
 task=asyncio.create_task(loop())
 context_task=asyncio.create_task(intelligence_loop())
 # Paper-trading worker disabled: prediction-only mode.
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
 context_task.cancel()
 pending_predictions=list(_prediction_tasks)
 for pending in pending_predictions:pending.cancel()
 for running in (task,history_task,context_task,*pending_predictions):
  try: await running
  except asyncio.CancelledError: pass
historical_state={"status":"pending","requested":672,"period":HISTORY_START.isoformat()+" through "+HISTORY_END.isoformat()+" (exclusive)","verified":0}
app=FastAPI(lifespan=lifespan)
@app.get("/research-dashboard.js")
def dashboard_script():
 return FileResponse(str(Path(__file__).with_name("research_dashboard.js")),media_type="application/javascript")

@app.get("/api/paper")
def paper_status():
 try:return {**paper.report(execute),"monitor_error":state.get("paper_error")}
 except Exception:return JSONResponse({"mode":"PAPER_ONLY","error":"Paper ledger unavailable"},status_code=503)

@app.post("/api/paper/control")
async def paper_control(request:Request):
 from urllib.parse import urlparse
 origin=urlparse(request.headers.get("origin",""))
 if origin.scheme not in ("http","https") or origin.netloc!=request.headers.get("host") or request.headers.get("x-paper-control")!="1":
  return JSONResponse({"error":"Same-origin paper controls required"},status_code=403)
 try:
  body=await request.json()
  action=body.get("action") if isinstance(body,dict) else None
  if action not in ("pause","resume","stop"):return JSONResponse({"error":"Only paper pause, resume or stop are supported"},status_code=400)
  if action=="resume" and os.getenv("PAPER_TRADING_ENABLED","true").lower() not in ("true","1","yes"):
   return JSONResponse({"error":"Paper trading is disabled by the server setting"},status_code=409)
  paper.control(connect,bool(DB),action)
  return {"mode":"PAPER_ONLY","action":action,"real_orders_enabled":False}
 except Exception:return JSONResponse({"error":"Paper control unavailable"},status_code=503)

@app.get("/api/research")
def get_research():
 try:return research_report()
 except Exception:return JSONResponse({"error":"Research database unavailable"},status_code=503)

@app.get("/api/research.csv")
def research_csv():
 import csv,io
 out=io.StringIO();writer=csv.writer(out)
 writer.writerow(["market","checkpoint","captured_at","strategy","direction","source","confidence","winner","result","entry_price","assumed_slippage","paper_fee_usd","paper_net_pnl_usd","evidence_json"])
 for market,checkpoint,created,snapshot,calls_json,winner in research_rows():
  for strategy,call in json.loads(calls_json).items():
   direction=call.get("direction");cost=call.get("entry_cost") or {};eligible=direction in ("UP","DOWN") and (strategy!="AI" or call.get("source")=="AI_REVIEW")
   result=("WIN" if direction==winner else "LOSS") if winner and eligible else "PENDING" if eligible else "NO_CALL"
   pnl=cost.get("net_if_win_usd" if result=="WIN" else "net_if_loss_usd") if result in ("WIN","LOSS") and cost.get("status")=="estimated" else None
   fields=[market,checkpoint,created,strategy,direction,call.get("source"),call.get("confidence"),winner,result,cost.get("quoted_entry"),cost.get("assumed_slippage_per_contract"),cost.get("fee_usd"),pnl,snapshot]
   writer.writerow([("'"+v if v.startswith(("=","+","-","@")) else v) if isinstance(v,str) else v for v in fields])
 return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=btc_15m_forward_research.csv"})

@app.get("/api/entry-estimate")
def estimate_entry(direction:str="UP",contracts:float=Query(100,gt=0,le=1000000),slippage:float=Query(.005,ge=0,le=.25)):
 if direction not in ("UP","DOWN"):return JSONResponse({"error":"Direction must be UP or DOWN"},status_code=400)
 quote=state.get("market_quote") or {}
 if not state.get("market") or quote.get("market")!=state.get("market") or quote.get("status")!="available" or time.time()-quote.get("fetched_epoch",0)>30:return {"status":"unavailable","reason":"No fresh executable-side quote"}
 return {"market":state.get("market"),"direction":direction,**entry_cost(quote.get("up_entry" if direction=="UP" else "down_entry"),contracts,slippage)}

@app.get("/api/trading-diagnostics")
def trading_diagnostics():
 """Read-only paper-trading health check. Never submits a real order."""
 now=time.time()
 quote=state.get("market_quote") or {}
 ai=paper_signal_state()
 market=state.get("market")
 age=lambda v:round(now-float(v),1) if isinstance(v,(int,float)) and v>0 else None
 qage=age(quote.get("fetched_epoch"))
 aiage=age(ai.get("captured_epoch"))
 current=market and quote.get("market")==market
 try:
  skips=execute("SELECT reason,COUNT(*) FROM paper_attempts WHERE status='SKIPPED' GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 8",fetch=True)
  fills=execute("SELECT COUNT(*) FROM paper_positions WHERE status='OPEN'",fetch=True)[0][0]
  unfilled=execute("SELECT COUNT(*) FROM paper_orders WHERE status='UNFILLED'",fetch=True)[0][0]
  last=execute("SELECT created_at,status,reason FROM paper_attempts ORDER BY created_at DESC LIMIT 1",fetch=True)
  database_error=None
 except Exception as exc:
  skips=[];fills=None;unfilled=None;last=[];database_error=type(exc).__name__+": "+str(exc)[:150]
 blockers=[]
 if not market:blockers.append("No verified active Polymarket US 15-minute market")
 if quote.get("status")!="available" or not current or qage is None or qage>15:blockers.append("Polymarket quote missing, stale, or for another round")
 if ai.get("status")!="ready" or aiage is None or aiage>60:blockers.append("No fresh AI paper-trading signal")
 if (ai.get("snapshot") or {}).get("reference_price",{}).get("status")!="fresh":blockers.append("AI snapshot lacks fresh official reference evidence")
 if state.get("paper_error"):blockers.append("Paper worker error: "+str(state["paper_error"])[:120])
 if database_error:blockers.append("Paper database error")
 return {"mode":"PAPER_ONLY","checked_epoch":now,"market":market,
  "market_lookup_reason":state.get("market_lookup_reason"),"quote_status":quote.get("status"),
  "quote_age_seconds":qage,"quote_matches_market":bool(current),
  "ai_status":ai.get("status"),"ai_age_seconds":aiage,
  "ai_direction":ai.get("verdict"),"ai_profit_action":(ai.get("profit_opportunity") or {}).get("action"),
  "paper_worker_error":state.get("paper_error"),"quote_storage_error":state.get("quote_storage_error"),
  "monitor_cycle_seconds":state.get("last_cycle_duration_seconds"),
  "open_positions":fills,"unfilled_exit_orders":unfilled,
  "top_entry_blockers":[{"reason":why,"count":count} for why,count in skips],
  "last_attempt":{"created_at":last[0][0],"status":last[0][1],"reason":last[0][2]} if last else None,
  "database_error":database_error,"active_blockers":blockers,
  "diagnosis":"READY_TO_EVALUATE" if not blockers else "BLOCKED_OR_DEGRADED",
  "note":"This endpoint checks signal and feed readiness, not predictive accuracy or guaranteed profitability."}

@app.get("/api/health")
def health():
 try:
  execute("SELECT 1",fetch=True)
  alerts=health_checks(state,public_state(),DB)
  return {"status":"degraded" if alerts else "ok","checked_epoch":time.time(),"alerts":alerts,"history":alert_history(execute),"persistent_database":bool(DB),"monitor_updated":state.get("updated")}
 except Exception:return JSONResponse({"status":"degraded","alerts":[{"code":"DATABASE","severity":"critical","message":"Database unavailable"}]},status_code=503)

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
@app.get("/api/brti-chart")
def brti_chart():
  """BRTI reference observations from Polymarket US asset-prices API, not CF site scraping."""
  chart=state.get("brti_chart") or {}
  market=state.get("market")
  if not market or chart.get("market")!=market:
   return {"status":"unavailable","points":[],"reason":"No current 15-minute BRTI reference observations"}
  age=time.time()-float(chart.get("observed_epoch") or 0)
  points=chart.get("points") or []
  return {"status":"fresh" if points and age>=0 and age+float(chart.get("sample_age_seconds") if chart.get("sample_age_seconds") is not None else 999)<=15 else "stale" if points else "unavailable",
          "market":market,"points":points,"source":"Polymarket US BTC reference / BRTI",
          "official_target":number((state.get("target_behavior") or {}).get("target")),
          "observed_age_seconds":round(age,1),
          "note":"BRTI reference index values supplied by Polymarket US. Not a direct CF Benchmarks feed, nor an embedded CF Benchmarks chart. Original site: https://www.cfbenchmarks.com/data/indices/BRTI."}

@app.get("/api/polymarket-chart")
def polymarket_chart():
  """Return Polymarket BBO quoted UP/DOWN asks only, not external BTC/USD prices."""
  market=state.get("market")
  if not market or "btc-updown-15m-" not in market:
   return {"status":"unavailable","reason":"No current verified Polymarket US 15-minute market","points":[]}
  start=market_start(market)
  if start is None:return {"status":"unavailable","reason":"Unrecognized market round","points":[]}
  rows=execute("""SELECT sampled_epoch,up_bid,up_ask,down_bid,down_ask,spread
    FROM polymarket_quote_samples WHERE market=? AND sampled_epoch>=?
    ORDER BY sampled_epoch ASC LIMIT 600""",(market,start),fetch=True)
  points=[dict(zip(("epoch","up_bid","up_ask","down_bid","down_ask","spread"),row)) for row in rows]
  quote=state.get("market_quote") or {}
  fresh=bool(quote.get("status")=="available" and quote.get("market")==market
             and time.time()-float(quote.get("fetched_epoch") or 0)<=20)
  return {"status":"live" if fresh else "stale","market":market,"points":points,
          "source":"Polymarket US verified 15-minute market BBO","round_start_epoch":start,
          "round_end_epoch":start+900,"latest_quote":quote if fresh else None,
          "note":"Quoted UP ask and complementary DOWN ask (1 - UP bid), NOT BTC/USD. Top-of-book indications, not guaranteed executable fills."}

_reference_candle_cache = {"at": 0.0, "payload": None}
_reference_candle_lock = asyncio.Lock()

@app.get("/api/candles")
async def candle_proxy():
 """Polymarket US reference OHLC; no exchange fallback or fabricated volume."""
 async with _reference_candle_lock:
  if time.monotonic()-_reference_candle_cache["at"]<5 and _reference_candle_cache["payload"] and int(time.time()//60)==_reference_candle_cache.get("minute"):
   return _reference_candle_cache["payload"]
  source="Polymarket US BTC reference"
  try:
   async with httpx.AsyncClient(timeout=8) as client:
    r=await client.get("https://gateway.polymarket.us/v1/asset-prices/history",params={
     "assets":"crypto:btc","from":int(time.time())-7200,"to":0,"bucket":"ASSET_PRICE_BUCKET_1M"})
    r.raise_for_status()
    series=next(x for x in r.json().get("series",[]) if
     x.get("asset",{}).get("symbol")=="btc" and
     x.get("asset",{}).get("assetClass")=="ASSET_CLASS_CRYPTO" and x.get("currency")=="USD")
    import math
    bars={}
    for x in series.get("candles",[]):
     if not isinstance(x,dict):continue
     try:
      if int(x.get("tickCount") or 0)<=0:continue
      t,lo,hi,op,cl=[float(x[k]) for k in ("timestamp","low","high","open","close")]
     except (KeyError,ValueError,TypeError,OverflowError):continue
     if not all(math.isfinite(v) for v in (t,lo,hi,op,cl)):continue
     if t%60 or t>time.time() or min(lo,hi,op,cl)<=0 or lo>min(op,cl) or hi<max(op,cl):continue
     bars[t]=[t,lo,hi,op,cl,0]
    candles=[bars[t] for t in sorted(bars)]
    if not candles:raise ValueError("No published reference candles")
    # Candle bucket timestamps bound freshness; the API has no last-tick timestamp.
    age=max(0,time.time()-(candles[-1][0]+60))
    payload={"source":source,"candles":candles,"status":"fresh" if age<=15 else "stale",
     "volume_available":False,"bucket_age_seconds":round(age,1),
     "note":"Published reference OHLC. No trading volume; zero-tick buckets omitted."}
  except Exception as e:
   if isinstance(e,httpx.HTTPStatusError):
    code="HTTP_"+str(e.response.status_code)
   elif isinstance(e,httpx.TimeoutException):code="TIMEOUT"
   elif isinstance(e,httpx.RequestError):code="CONNECTION_ERROR"
   else:code="INVALID_FEED_DATA"
   payload={"source":source,"candles":[],"status":"unavailable","volume_available":False,
    "error_code":code,"error":"Polymarket reference feed: "+code}
  _reference_candle_cache.update(at=time.monotonic(),payload=payload,minute=int(time.time()//60))
  return payload

@app.get("/api/status")
def status():
 try:
  rows=execute("SELECT result,COUNT(*) FROM signals WHERE phase='FINAL_15M' GROUP BY result",fetch=True)
  recent=execute("SELECT market,phase,direction,flip,score,created_at,result,quality,quality_reason,decision_source,decision_reason,ai_confidence FROM signals WHERE phase='FINAL_15M' ORDER BY created_at DESC LIMIT 50",fetch=True)
  ai_rows=execute("SELECT market,phase,direction,confidence,source,reason,created_at,result FROM ai_decisions WHERE phase='FINAL_15M' AND source='AI_REVIEW' ORDER BY created_at DESC LIMIT 50",fetch=True)
  # Show only the 100 most recently settled genuine AI calls in the scoreboard.
  # Older outcomes are retained intact for learning and longer-term research.
  recent_settled=execute("""SELECT result FROM ai_decisions
    WHERE phase='FINAL_15M' AND source='AI_REVIEW'
      AND result IN ('WIN','LOSS')
    ORDER BY created_at DESC, id DESC LIMIT 100""",fetch=True)
  wins=sum(result=='WIN' for (result,) in recent_settled)
  losses=sum(result=='LOSS' for (result,) in recent_settled)
  graded=wins+losses
  pending=execute("""SELECT COUNT(*) FROM ai_decisions
    WHERE phase='FINAL_15M' AND source='AI_REVIEW' AND result='PENDING'""",fetch=True)[0][0]
  verified_counts={"WIN":wins,"LOSS":losses,"PENDING":pending}
  ai_performance={"wins":wins,"losses":losses,"pending":pending,
                  "graded":graded,"window_size":100,
                  "accuracy_pct":round(100*wins/graded,1) if graded else None,
                  "win_loss_ratio":round(wins/losses,3) if losses else None,
                  "ratio_label":str(wins)+":"+str(losses),
                  "basis":"Most recent 100 settled genuine AI_REVIEW FINAL_15M predictions; pending calls excluded",
                  "unverified":pending}
  return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"counts":dict(rows),"ai_decision_counts":verified_counts,"ai_verified_counts":verified_counts,"ai_performance":ai_performance,"ai_decision_history":[dict(zip(("market","phase","direction","confidence","source","reason","created_at","result"),r)) for r in ai_rows],"accuracy_by_quality":accuracy_report(),"history":[dict(zip(("market","phase","direction","flip","score","created_at","result","quality","quality_reason","decision_source","decision_reason","ai_confidence"),r)) for r in recent]}
 except Exception as e: return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"database_error":str(e),"history":[]}
@app.get("/",response_class=HTMLResponse)
def home():
 return Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")



