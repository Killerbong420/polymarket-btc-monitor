import asyncio, json, os, sqlite3, time, uuid
from threading import RLock as _HistoryLock
from pathlib import Path
from historical_15m import main as collect_15m, START as HISTORY_START, END as HISTORY_END
from ai_confirmation import public_state, auth_state, paper_signal_state
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import httpx
from market_intelligence import intelligence_loop, public_intelligence
from market_chart_history import record_brti, snapshot as chart_history_snapshot, latest_tip, official_chart_sampler, eligible_tick_market
from research import VERSION, CHECKPOINTS, checkpoint_plan, reference_prices, target_behavior, market_quote, entry_cost, summarize, number, market_start, confluence_forecast
from price_action import analyze_price_action, shadow_price_action
from bot_library import study_strategies, consensus
from settlement_forecast import forecast_settlement, regime_shadow_forecast
from forecast_audit import summarize_forecast_audit
from forecast_guard import anchored_shadow_forecast, validate_official_target
from market_shock import measure_market_shock
from decision_engine import reversal_evidence, guarded_decision, manual_entry_review
from forward_eval import evaluate_guarded
from round_tiles import report as recent_round_tiles
from quick_forecast import quick_forecast
import five_min_monitor
from checkpoint_scoreboard import build_scoreboard
from quick_accuracy import make_entry as make_quick_entry, actual_tick_at, grade as grade_quick_call, summarize as summarize_quick_calls
from forward_training import training_readiness as score_training_readiness
from monitor_health import checks as health_checks, persist_alerts, alert_history, diagnostic_extras
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
_history_write_version=0
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
 global _history_write_version
 with connect() as c:
  if DB: sql = sql.replace("?", "%s")
  cur = c.cursor()
  try:
   cur.execute(sql, params)
   # Invalidate cached grading only after writes to the underlying scoring tables.
   # This avoids serving stale counts immediately after a settlement or new call.
   operation=sql.lstrip().split(None,1)[0].upper() if sql.strip() else ""
   if operation in ("INSERT","UPDATE","DELETE","REPLACE","CREATE","DROP","ALTER") and any(
      name in sql.lower() for name in ("signals","ai_decisions","five_min_","research_calls")):
    _history_write_version+=1
   if fetch: return cur.fetchall()
  finally:
   cur.close()
def iter_query_rows(sql, params=(), batch_size=64):
 """Stream large audit JSON rows instead of loading the full result into RAM.

 PostgreSQL uses a named server-side cursor so psycopg does not buffer the
 entire result in libpq; SQLite uses fetchmany. The cursor and connection
 are released when the caller exhausts the iterator.
 """
 if not isinstance(batch_size,int) or batch_size<1:
  raise ValueError("batch_size must be a positive integer")
 with connect() as conn:
  cur=conn.cursor(name="btc_audit_"+uuid.uuid4().hex) if DB else conn.cursor()
  try:
   cur.execute(sql.replace("?","%s") if DB else sql,params)
   while True:
    batch=cur.fetchmany(batch_size)
    if not batch:break
    for row in batch:yield row
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
 execute("""CREATE INDEX IF NOT EXISTS ai_recent_settled_idx
   ON ai_decisions(created_at DESC,id DESC)
   WHERE phase='FINAL_15M' AND source='AI_REVIEW' AND result IN ('WIN','LOSS')""")
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
 for column in ("final_reference_mean REAL",
                "final_reference_source TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'"):
  try: execute("ALTER TABLE research_calls ADD COLUMN "+column)
  except Exception as exc:
   if "duplicate column" not in str(exc).lower() and "already exists" not in str(exc).lower(): raise
 execute("CREATE INDEX IF NOT EXISTS research_market_idx ON research_calls(market)")
 paper.setup(execute)
 execute("""CREATE TABLE IF NOT EXISTS polymarket_quote_samples (
   id TEXT PRIMARY KEY, market TEXT NOT NULL,sampled_epoch REAL NOT NULL,
   up_bid REAL,up_ask REAL,down_bid REAL,down_ask REAL,spread REAL)""")
 execute("CREATE INDEX IF NOT EXISTS pm_quote_market_epoch ON polymarket_quote_samples (market,sampled_epoch)")
 execute("""CREATE TABLE IF NOT EXISTS quick_predictions (
   id TEXT PRIMARY KEY, market TEXT NOT NULL, minute_slot INTEGER NOT NULL,
   captured_epoch INTEGER NOT NULL, observed_epoch INTEGER NOT NULL,
   target_epoch INTEGER NOT NULL, starting_reference REAL NOT NULL,
   forecast_reference REAL NOT NULL, direction TEXT NOT NULL,
   quality TEXT NOT NULL, reversal_watch INTEGER NOT NULL,
   model_version TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'PENDING',
   final_reference REAL, scored_epoch INTEGER)""")
 execute("""CREATE INDEX IF NOT EXISTS quick_first5_graded_idx
   ON quick_predictions(captured_epoch DESC,id DESC)
   WHERE result IN ('WIN','LOSS')""")
 execute("""CREATE INDEX IF NOT EXISTS quick_pending_market_idx
   ON quick_predictions(market,target_epoch) WHERE result='PENDING'""")
_quick_seen_market=None
_quick_seen_slots=set()
def track_first_five_quick(slug, reference, quick, now_epoch):
 """Persist first 5 market-minute calls; grade exact BRTI ticks a minute later."""
 global _quick_seen_market
 if _quick_seen_market != slug:
  _quick_seen_slots.clear()
  _quick_seen_market = slug
 entry=make_quick_entry(slug,market_start(slug),now_epoch,quick)
 if entry and entry["minute_slot"] not in _quick_seen_slots:
  execute("""INSERT INTO quick_predictions
    (id,market,minute_slot,captured_epoch,observed_epoch,target_epoch,
     starting_reference,forecast_reference,direction,quality,reversal_watch,
     model_version,result) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
     ON CONFLICT(id) DO NOTHING""",
     (entry["id"],entry["market"],entry["minute_slot"],entry["captured_epoch"],
      entry["observed_epoch"],entry["target_epoch"],entry["starting_reference"],
      entry["forecast_reference"],entry["direction"],entry["quality"],
      entry["reversal_watch"],entry["model_version"],entry["result"]))
  _quick_seen_slots.add(entry["minute_slot"])
 if reference.get("source")!="Polymarket US BTC reference / BRTI":
  return
 rows=execute("""SELECT id,target_epoch,starting_reference,direction
   FROM quick_predictions WHERE market=? AND result='PENDING' AND target_epoch<=?
   ORDER BY target_epoch LIMIT 8""",(slug,int(now_epoch)),fetch=True)
 for ident,target_epoch,start_price,direction in rows:
  end_price=actual_tick_at(reference,target_epoch,now_epoch)
  if end_price is None: continue
  result=grade_quick_call(start_price,direction,end_price)
  if result is None: continue
  execute("""UPDATE quick_predictions SET result=?,final_reference=?,scored_epoch=?
    WHERE id=? AND result='PENDING'""",(result,end_price,int(now_epoch),ident))

def first_five_quick_report():
 rows=execute("""SELECT result,minute_slot FROM quick_predictions
   WHERE result IN ('WIN','LOSS') ORDER BY captured_epoch DESC,id DESC LIMIT 100""",fetch=True)
 counts=dict(execute("""SELECT result,COUNT(*) FROM quick_predictions
   WHERE result IN ('PENDING','NO_CALL','TIE') GROUP BY result""",fetch=True))
 summary=summarize_quick_calls(rows,pending=counts.get("PENDING",0),
   no_call=counts.get("NO_CALL",0),ties=counts.get("TIE",0))
 recent=execute("""SELECT market,minute_slot,captured_epoch,direction,quality,
    result,starting_reference,final_reference FROM quick_predictions
    ORDER BY captured_epoch DESC,id DESC LIMIT 12""",fetch=True)
 summary["recent"]=[dict(zip(("market","minute_slot","captured_epoch","direction",
   "quality","result","starting_reference","final_reference"),r)) for r in recent]
 return summary

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
   if slug not in (event_slug,"cpc-"+event_slug): continue
   if time.time()>=(start+timedelta(minutes=15)).timestamp():
    return None,"round_expired_during_lookup"
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
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-4]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence(candles,5),"price_action":analyze_price_action(candles,"Coinbase Exchange BTC-USD"),"bot_research_signals":study_strategies(candles,"Coinbase Exchange BTC-USD"),"source":"Coinbase Exchange BTC-USD","market_shock":measure_market_shock(candles,source="Coinbase Exchange BTC-USD"),"candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
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
  return {"price":closes[-1],"momentum_pct":round((closes[-1]/closes[-4]-1)*100,4),"long_momentum_pct":round((closes[-1]/closes[-16]-1)*100,4),"pressure":0.0,"pressure_available":False,"volume_ratio":round(min(10,recent/baseline),2),"chart":chart,"chart_pattern":pattern,"graph_evidence":graph_evidence([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],5),"price_action":analyze_price_action([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],"Kraken BTC/USD"),"bot_research_signals":study_strategies([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],"Kraken BTC/USD"),"source":"Kraken BTC/USD","market_shock":measure_market_shock([[b[0],b[3],b[2],b[1],b[4],b[6]] for b in candles],source="Kraken BTC/USD"),"candle_age_seconds":round(time.time()-float(candles[-1][0])-60)}
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

def official_numeric_price(market, winner):
 """Numeric scoring uses only the official resolved BTC BRTI market settlement."""
 if not isinstance(market,dict) or winner not in ("UP","DOWN"):return None
 status=str(market.get("status") or market.get("marketStatus") or "").upper()
 if "RESOLVED" not in status or "UNRESOLVED" in status:return None
 terms=market.get("assetPriceTerms") or {}
 if not isinstance(terms,dict) or terms.get("marketType")!="ASSET_PRICE_MARKET_TYPE_UP_DOWN":return None
 asset=terms.get("asset") or {}
 if not isinstance(asset,dict) or str(asset.get("symbol") or "").lower()!="btc":return None
 if terms.get("indexSymbol")!="BRTI":return None
 sides=market.get("marketSides") or []
 if not isinstance(sides,list) or len(sides)!=2:return None
 if {str(s.get("description") or "").lower() for s in sides if isinstance(s,dict)}!={"yes","no"}:return None
 if {s.get("long") for s in sides if isinstance(s,dict)}!={True,False}:return None
 target=number(terms.get("priceToBeat"))
 official=number(terms.get("settlementPrice"))
 if target is None or official is None or target<=0 or official<=0:return None
 if ("UP" if official>=target else "DOWN")!=winner:return None
 return round(official,2)


_reference_retry_after={}
async def retry_numeric_final_means(client, max_requests=3, now=None):
 """Backfill official published settlement prices; never use chart reconstructions.

 Legacy numeric values were sourced from the public 1s chart, which can differ
 from the published market settlement. Revalidate them before numeric scoring.
 """
 now=time.time() if now is None else now
 result={"status":"checked","requested":0,"graded":0,"incomplete":0}
 rows=(await asyncio.to_thread(execute, """SELECT DISTINCT market,winner FROM research_calls
      WHERE winner IN ('UP','DOWN')
      AND (final_reference_mean IS NULL OR final_reference_source!='OFFICIAL_MARKET_SETTLEMENT')
      ORDER BY market DESC LIMIT 150""",fetch=True))
 for market,win in rows:
  start=market_start(market)
  if start is None or not (start+960<=now<=start+8*86400):continue
  if now < _reference_retry_after.get(market,0):continue
  if result["requested"]>=max_requests:break
  result["requested"]+=1
  try:
   stamp=market.split("btc-updown-15m-",1)[-1]
   r=await client.get("https://gateway.polymarket.us/v1/events/slug/btc-updown-15m-"+stamp,timeout=10)
   r.raise_for_status()
   raw=r.json()
   event=raw.get("event",raw) if isinstance(raw,dict) else {}
   contracts=event.get("markets",[]) if isinstance(event,dict) else []
   if not contracts and isinstance(raw,dict):contracts=raw.get("markets",[])
   candidate=next((m for m in contracts if isinstance(m,dict)
       and str(m.get("slug") or m.get("marketSlug") or "")==market),None)
   official=official_numeric_price(candidate,win)
   if official is None:
    result["incomplete"]+=1
    _reference_retry_after[market]=now+600
    continue
   (await asyncio.to_thread(execute, """UPDATE research_calls SET final_reference_mean=?,
       final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
       WHERE market=? AND winner=?""",(official,market,win)))
   result["graded"]+=1
   _reference_retry_after.pop(market,None)
  except Exception:
   result["incomplete"]+=1
   _reference_retry_after[market]=now+600
 # Bound retry cache over long-running sessions.
 for market,until in list(_reference_retry_after.items()):
  start=market_start(market)
  if start is None or start+8*86400<now: _reference_retry_after.pop(market,None)
 return result

async def settle(client):
 # Resolve the original event when the market-slug endpoint is unavailable.
 # Never infer official outcomes from Coinbase spot candles.
 from datetime import timedelta
 pending=(await asyncio.to_thread(execute, "SELECT market FROM signals WHERE result='PENDING' AND phase='FINAL_15M' AND market LIKE ? UNION SELECT market FROM ai_decisions WHERE result='PENDING' AND phase='FINAL_15M' AND source='AI_REVIEW' UNION SELECT market FROM research_calls WHERE winner IS NULL UNION SELECT market FROM paper_positions WHERE status='OPEN' ORDER BY market DESC",("%btc-updown-15m-%",),fetch=True))
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
   (await asyncio.to_thread(execute, "UPDATE signals SET result=CASE WHEN direction='WAIT' THEN 'NO_CALL' WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING'",(win,slug)))
   (await asyncio.to_thread(execute, "UPDATE ai_decisions SET result=CASE WHEN direction=? THEN 'WIN' ELSE 'LOSS' END WHERE market=? AND result='PENDING'",(win,slug)))
   (await asyncio.to_thread(execute, "UPDATE research_calls SET winner=?,settled_at=? WHERE market=? AND winner IS NULL",(win,datetime.now(timezone.utc).isoformat(),slug)))
   official=official_numeric_price(market,win)
   if official is not None:
    (await asyncio.to_thread(execute, """UPDATE research_calls SET final_reference_mean=?,
        final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
        WHERE market=? AND winner=?""",(official,slug,win)))
    diagnostic["official_numeric_recorded"]=diagnostic.get("official_numeric_recorded",0)+1
   # Numeric scoring may backfill from the published market value when absent.
   # The official binary winner is never inferred from a public chart reconstruction.
   (await asyncio.to_thread(execute, "UPDATE paper_positions SET winner=? WHERE market=? AND status='OPEN'",(win,slug)))
   diagnostic["verified"]+=1
  except Exception as exc:
   diagnostic["errors"].append(type(exc).__name__+": "+str(exc)[:100])
 diagnostic["overdue_markets"]=sum(1 for (slug,) in pending if market_start(slug) is not None and time.time()>market_start(slug)+2700)
 try: diagnostic["numeric_price_backfill"]=await retry_numeric_final_means(client)
 except Exception as exc:diagnostic["numeric_price_backfill"]={"status":"error","reason":type(exc).__name__}
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
 """Yield bounded research JSON rows; never fetch thousands into a list."""
 return iter_query_rows("SELECT market,checkpoint,created_at,snapshot_json,calls_json,winner FROM research_calls ORDER BY created_at DESC LIMIT 10000")

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
 # Two bounded cursor passes avoid retaining thousands of large BRTI JSON
 # snapshots across every new 15-minute round. Both passes exclude the
 # current market and any unverified outcome, preserving forward-only audits.
 def settled_rows():
  return (row for row in research_rows() if row[0]!=market and row[5] in ('UP','DOWN'))
 report=summarize(settled_rows())
 feedback={"source":"settled forward prediction log","excluded_market":market,
  "strategies":report.get("strategies",[]),"confidence":report.get("confidence",[]),
  "paired_comparisons":report.get("paired_comparisons",[]),
  "loss_review":prediction_loss_review(settled_rows()),
  "limitations":"Small observational samples across multiple code versions. Repeated checkpoints share rounds. Confidence labels are not calibrated probabilities. Do not automatically select the highest historical accuracy or infer trading profitability. Official published target overrides reconstructed values."}
 _outcome_feedback_cache.clear();_outcome_feedback_cache[market]=(time.time(),feedback)
 return feedback

_feedback_task=None

def live_outcome_feedback(market):
 """Use same-round cached learning without delaying fresh price acquisition."""
 global _feedback_task
 cached=_outcome_feedback_cache.get(market)
 if (not cached or time.time()-cached[0]>=60) and (_feedback_task is None or _feedback_task.done()):
  async def refresh():
   try:
    await asyncio.to_thread(outcome_feedback,market)
    state["feedback_error"]=None
   except Exception as exc:
    state["feedback_error"]=type(exc).__name__
  _feedback_task=track_prediction(asyncio.create_task(refresh()))
 return cached[1] if cached else {"status":"collecting","excluded_market":market,
  "reason":"Historical learning is refreshing independently of live prices"}

async def capture_checkpoint(client,slug,checkpoint,left,d,primary,flip,score,market):
 key=slug+":"+checkpoint
 if (await asyncio.to_thread(execute, "SELECT id FROM research_calls WHERE id=?",(key,),fetch=True)):return
 # Freeze common evidence BEFORE the AI call; later price changes cannot rewrite it.
 import copy
 frozen=copy.deepcopy(d);captured=datetime.now(timezone.utc).isoformat()
 target=number(((market.get("assetPriceTerms") or {}).get("priceToBeat")))
 behavior=frozen.get("target_behavior") or {}
 quote=frozen.get("market_quote") or {}
 fallback=final_direction(frozen,primary,score)
 review={"direction":fallback,"source":"RULE_BASED","ai_confidence":"UNRATED","reason":"AI disabled; deterministic indicators and BRTI price-path forecast","forecast_final_mean_usd":(frozen.get("settlement_forecast") or {}).get("forecast_final_mean_usd")}
 chosen=review.get("direction") if review.get("direction") in ("UP","DOWN") else fallback
 confidence=review.get("ai_confidence","UNRATED")
 quality,quality_reason=signal_quality_snapshot(frozen,primary,score)
 calls={
  "AI":{"direction":chosen if review.get("source")=="AI_REVIEW" else "WAIT","source":review.get("source"),"confidence":confidence,"reason":review.get("reason"),"forecast_final_mean_usd":review.get("forecast_final_mean_usd"),"forecast_rationale":review.get("forecast_rationale"),"news_assessment":review.get("news_assessment")},
  "INDICATORS":{"direction":primary,"source":"INDICATORS","confidence":quality,"reason":quality_reason},
  "INDICATOR_BINARY":{"direction":fallback,"source":"FORCED_INDICATORS","confidence":quality},
  "TARGET_SIDE":{"direction":behavior.get("side") if behavior.get("status")=="available" and behavior.get("side") in ("UP","DOWN") else "WAIT","source":behavior.get("source"),"confidence":"UNRATED"}}
 path=frozen.get("settlement_forecast") or {}
 calls["PRICE_PATH"]={"direction":path.get("projected_direction") if path.get("status")=="available" and path.get("projected_direction") in ("UP","DOWN") else "WAIT",
  "source":path.get("version"),"confidence":"UNRATED","forecast_final_mean_usd":path.get("forecast_final_mean_usd"),
  "reason":"Independent future BRTI final-minute projection, not the current target side"}
 regime=frozen.get("time_regime_shadow") or {}
 calls["REGIME_SHADOW"]={"direction":regime.get("projected_direction") if regime.get("status")=="available" else "WAIT",
  "source":regime.get("version"),"confidence":"UNRATED","forecast_final_mean_usd":regime.get("forecast_final_mean_usd"),
  "reason":"Unvalidated time-aware trend-shrink shadow forecast"}
 a_price=number(calls["AI"].get("forecast_final_mean_usd"))
 p_price=number(calls["PRICE_PATH"].get("forecast_final_mean_usd"))
 calls["AI"]["independent_forecast"]=False
 calls["AI"]["price_path_informed"]=bool(review.get("price_path_informed"))
 calls["AI"]["analysis_context_version"]=review.get("analysis_context_version") or VERSION
 calls["AI"]["forecast_status"]=review.get("forecast_status","LEGACY_OR_UNVERIFIED")
 calls["AI"]["forecast_was_direction_decider"]=bool(review.get("forecast_was_direction_decider"))
 calls["AI"]["matches_price_path_to_cent"]=(round(a_price,2)==round(p_price,2)) if a_price is not None and p_price is not None else None
 calls["AI"]["price_path_delta_usd"]=round(a_price-p_price,2) if a_price is not None and p_price is not None else None
 # Fixed conservative research shadow: records a bounded blend of the
 # independent BRTI path and AI estimate without modifying the AI call.
 shadow=anchored_shadow_forecast(a_price,path,frozen.get("reference_price"),target)
 guarded=guarded_decision(path,frozen.get("reference_price"),target,left,a_price,frozen.get("reversal_evidence"))
 calls["GUARDED_PRIMARY"]={**guarded,"source":guarded.get("version"),
  "confidence":guarded.get("quality","UNRATED"),
  "entry_review":manual_entry_review(guarded,quote,slug)}
 calls["ANCHORED_SHADOW"]={
  "direction":shadow.get("projected_direction","WAIT"),
  "source":shadow.get("version"),
  "confidence":"UNRATED",
  "forecast_final_mean_usd":shadow.get("forecast_final_mean_usd"),
  "status":shadow.get("status"),
  "reason":shadow.get("reason") or shadow.get("note"),
  "ai_was_clipped":shadow.get("ai_was_clipped"),
 }
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
 snapshot={"version":review.get("analysis_context_version") or VERSION,"captured_at":captured,"seconds_left":left,"checkpoint":checkpoint,
  "official_target":target,"target_behavior":behavior,"reference_price":frozen.get("reference_price"),
  "market_quote":quote,"chart_pattern":frozen.get("chart_pattern"),"price_action":frozen.get("price_action"),"bot_research_signals":frozen.get("bot_research_signals"),"graph_evidence":frozen.get("graph_evidence"),
  "external_intelligence":frozen.get("external_intelligence"),"news_assessment":review.get("news_assessment"),
  "outcome_feedback":frozen.get("outcome_feedback"),"settlement_forecast":frozen.get("settlement_forecast"),"time_regime_shadow":frozen.get("time_regime_shadow"),"brti_recent_samples":frozen.get("brti_recent_samples"),"target_validation":frozen.get("target_validation"),"indicator_score":score,"ai_model":review.get("model"),"historical_context":(review.get("evidence") or {}).get("historical_context")}
 # The checkpoint and its legacy halfway rows commit together or roll back together.
 def save_checkpoint():
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
 await asyncio.to_thread(save_checkpoint)
 if checkpoint=="HALFWAY":state["last_guarded_primary"]={**calls["GUARDED_PRIMARY"],"market":slug,"checkpoint":checkpoint,"captured_at":captured}
 if checkpoint=="HALFWAY":state["last_halfway_ai"]={"market":slug,"phase":"FINAL_15M","direction":chosen,"price_path_informed":bool(review.get("price_path_informed")),"analysis_context_version":review.get("analysis_context_version") or VERSION,"forecast_final_mean_usd":review.get("forecast_final_mean_usd"),"indicator_direction":fallback,"source":review.get("source"),"reason":review.get("reason"),"ai_confidence":confidence,"updated":captured,"news_assessment":review.get("news_assessment")}
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
 # Each database operation owns its connection inside the worker thread.
 # Synchronous Postgres I/O must never stall HTTP or the BRTI sampler.
 (await asyncio.to_thread(setup))
 ai_task=None
 settlement_task=None
 last_settlement_check=None
 checkpoints={}
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
  while True:
   cycle_started=time.monotonic()
   try:
    state["loop_started_epoch"]=time.time()
    spot_result,market_result=await asyncio.gather(
     spot_data(client),market_15m(client),return_exceptions=True)
    if isinstance(spot_result,BaseException):
     d={"source":"unavailable","candle_age_seconds":None,"signal_quality":{"status":"INSUFFICIENT_DATA","reason":"No fresh verified BTC candles"},"feed_error":str(spot_result)[:300]}
    else:d=spot_result
    if isinstance(market_result,BaseException):
     fifteen,reason15=None,"lookup_"+type(market_result).__name__
    else:fifteen,reason15=market_result
    state["market_lookup_reason"]=reason15
    state["ai_auth_error"]=None
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
     try:d["outcome_feedback"]=live_outcome_feedback(slug)
     except Exception:d["outcome_feedback"]={"status":"unavailable"}
     terms=fifteen.get("assetPriceTerms") or {}
     ptb=terms.get("priceToBeat") or {}
     target=number(ptb)  # Accept official target whether API supplies a scalar or {"value": ...}.
     reference,quote=await asyncio.gather(reference_prices(client,fifteen),market_quote(client,slug))
     left15=fifteen["end"]-time.time()
     if left15<=0:
      raise ValueError("Round expired during reference lookup")
     phase="EARLY" if left15>450 else "LATE" if left15<=180 else "MID"
     state.update(phase=phase,seconds_left=round(left15))
     state["final_15m"]["seconds_left"]=round(left15)
     # Display-only BRTI reference series from the Polymarket US reference API.
     # Keep raw index ticks in process memory; no CF Benchmarks site scraping.
     ref_points=reference.get("points") or []
     if reference.get("status")=="fresh":record_brti(ref_points,reference.get("source"))
     if reference.get("status") in ("fresh","stale") and ref_points:
      state["brti_chart"]={"market":slug,"source":reference.get("source"),
       "points":[[int(t),round(float(p),2)] for t,p in ref_points if t>=time.time()-16*60][::3],
       "sample_age_seconds":reference.get("age_seconds"),
       "observed_epoch":time.time()}
     else:state["brti_chart"]={"market":slug,"source":"Polymarket US reference API",
       "points":[],"status":"unavailable","reason":reference.get("reason","No verified BRTI observations")}
     ref_public={k:v for k,v in reference.items() if k!="points"}
     # Compact genuine timestamped official BRTI samples, NOT a projected path.
     # Do not fill gaps with synthetic observations; keep the payload bounded.
     recent_points=[(int(t),round(float(p),2)) for t,p in ref_points
                    if time.time()-600<=float(t)<=time.time()]
     brti_samples=[(t,p) for t,p in recent_points if t%30==0][-20:]
     if recent_points and (not brti_samples or brti_samples[-1][0]!=recent_points[-1][0]):
      brti_samples.append(recent_points[-1])
     d["brti_recent_samples"]={"source":reference.get("source"),
       "timeframe":"observed_30s_samples","points":brti_samples,"last_tick_epoch":recent_points[-1][0] if recent_points else None,
       "no_missing_ticks_interpolated":True}
     behavior=target_behavior(d,target,slug,reference)
     forecast_now=time.time()
     quick=quick_forecast(reference,now=forecast_now)
     try:
      (await asyncio.to_thread(track_first_five_quick, slug,reference,quick,forecast_now))
      state["quick_accuracy_error"]=None
     except Exception as quick_exc:
      # A logging issue must never freeze the live 15-minute prediction loop.
      state["quick_accuracy_error"]=type(quick_exc).__name__
     price_path=forecast_settlement(reference,d,target,left15,now=forecast_now)
     regime_shadow=regime_shadow_forecast(reference,d,target,left15,now=forecast_now)
     reversal=reversal_evidence(reference,left15)
     guarded_live=guarded_decision(price_path,ref_public,target,left15,reversal=reversal)
     entry_review=manual_entry_review(guarded_live,quote,slug)
     d.update(reference_price=ref_public,target_behavior=behavior,market_quote=quote,settlement_forecast=price_path,time_regime_shadow=regime_shadow,quick_forecast=quick,reversal_evidence=reversal,guarded_primary=guarded_live,manual_entry_review=entry_review)
     state.update(reference_price=ref_public,target_behavior=behavior,market_quote=quote,settlement_forecast=price_path,quick_forecast=quick,reversal_evidence=reversal,guarded_primary=guarded_live,manual_entry_review=entry_review)
     try:
      (await asyncio.to_thread(capture_polymarket_quote, quote))
      state["quote_storage_error"]=None
     except Exception as quote_exc:
      state["quote_storage_error"]=type(quote_exc).__name__+": "+str(quote_exc)[:150]
     avg=reference.get("opening_average");pt=number(target)
     state["target_validation"]=validate_official_target(pt,reference)
     d["target_validation"]=state["target_validation"]
     captured={r[0] for r in (await asyncio.to_thread(execute, "SELECT checkpoint FROM research_calls WHERE market=?",(slug,),fetch=True))}
     plan=checkpoint_plan(left15,captured)
     state["missed_checkpoints"]=plan["missed"]
     for name in plan["due"]:
      if d.get("price") is not None:
       key=(slug,name)
       if key not in checkpoints or checkpoints[key].done():
        checkpoints[key]=track_prediction(asyncio.create_task(capture_checkpoint(client,slug,name,round(left15),json.loads(json.dumps(d)),direction,flip,score,fifteen)))
     checkpoints={k:v for k,v in checkpoints.items() if not v.done()}
     d["paper_position"]=(await asyncio.to_thread(paper.position_context, execute,slug))
     state["ai_disabled"]=True
    else:
     for key in ("phase","seconds_left","score"): state.pop(key,None)
     state.update(status="market unavailable",signal="WAIT",flip=flip,market=None,external=d,updated=now,error="No verified Polymarket US 15-minute market: "+str(reason15))
     state["final_15m"]={"market":None,"lookup":reason15,"trigger_seconds":450}
     state.update(reference_price={"status":"unavailable"},target_behavior={"status":"unavailable"},market_quote={"status":"unavailable"},settlement_forecast={"status":"unavailable","reason":"No verified 15-minute contract"},quick_forecast={"status":"unavailable","reason":"No verified 15-minute contract"},guarded_primary={"status":"unavailable","direction":"WAIT"},manual_entry_review={"status":"unavailable"},reversal_evidence={"status":"unavailable"},missed_checkpoints=[])
     state["ai_disabled"]=True
    state["feed_health"]={"status":"FRESH" if d.get("price") is not None else "UNAVAILABLE","source":d.get("source"),"age_seconds":d.get("candle_age_seconds"),"error":d.get("feed_error")}
    # Historical settlement can involve 30 remote rounds and must not
    # prevent current 15-minute predictions from refreshing every cycle.
    # First settlement sweep must start immediately, even when a host's
    # monotonic clock has been running for less than the 30-second interval.
    # Read failures from completed tasks so they cannot silently stop grading.
    if settlement_task is not None and settlement_task.done():
     try:
      settlement_error=settlement_task.exception()
      state["settlement_task_error"]=(type(settlement_error).__name__+": "+str(settlement_error)[:180]) if settlement_error else None
     except asyncio.CancelledError:
      state["settlement_task_error"]="cancelled"
    if (settlement_task is None or settlement_task.done()) and (last_settlement_check is None or time.monotonic()-last_settlement_check>=30):
     settlement_task=track_prediction(asyncio.create_task(settle(client)))
     last_settlement_check=time.monotonic()
    state["settlement_task_running"]=settlement_task is not None and not settlement_task.done()
    state["alerts"]=health_checks(state,public_state(),DB)
    (await asyncio.to_thread(persist_alerts, execute,state["alerts"]))
   except Exception as e:
    for key in ("phase","seconds_left","score"): state.pop(key,None)
    state.update(status="feed error",signal="WAIT",flip="WAIT",quick_forecast={"status":"unavailable","reason":"Monitoring feed error"},error=type(e).__name__+": "+str(e)[:200],updated=datetime.now(timezone.utc).isoformat())
    state.update(reference_price={"status":"unavailable"},market_quote={"status":"unavailable"},
                 settlement_forecast={"status":"unavailable","reason":"Monitoring feed error"},
                 guarded_primary={"status":"unavailable","direction":"WAIT"},
                 manual_entry_review={"status":"unavailable"})
    db_error=None
    try:(await asyncio.to_thread(execute, "SELECT 1",fetch=True))
    except Exception:db_error="Database query failed"
    state["alerts"]=health_checks(state,public_state(),DB,db_error=db_error)
   state["last_cycle_duration_seconds"]=round(time.monotonic()-cycle_started,2)
   state["updated"]=datetime.now(timezone.utc).isoformat()
   try:await asyncio.sleep(max(0, POLL - (time.monotonic() - cycle_started)))
   except asyncio.CancelledError:
    if ai_task is not None:
     ai_task.cancel()
    if settlement_task is not None:
     settlement_task.cancel()
    raise
async def paper_worker():
 async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchPaperSandbox/1.0"}) as client:
  while True:
   started=time.monotonic()
   try:
    await paper.cycle(client,execute,connect,bool(DB),{"status":"disabled","direction":"WAIT","live_quote":state.get("market_quote")})
    state["paper_error"]=None
    state["paper_unfilled_exits"]=(await asyncio.to_thread(execute, "SELECT COUNT(*) FROM paper_orders WHERE status='UNFILLED'",fetch=True))[0][0]
   except Exception as exc:state["paper_error"]=type(exc).__name__+": "+str(exc)[:160]
   await asyncio.sleep(max(0,1-(time.monotonic()-started)))

async def read_only_diagnostics_loop():
 """Publish aggregates to private Render logs without opening Postgres to the internet."""
 await asyncio.sleep(15)
 while True:
  try:
   def gather():
    current=status()
    prices=numeric_price_forecast_report()
    return {
     "checked_utc":datetime.now(timezone.utc).isoformat(),
     "prediction_model_version":VERSION,
     "service_status":state.get("status"),
     "persistent_database":bool(DB),
     "market_active":bool(state.get("market")),
     "reference_status":(state.get("reference_price") or {}).get("status"),
     "monitor_cycle_seconds":state.get("last_cycle_duration_seconds"),
     "quick_forecast_status":(state.get("quick_forecast") or {}).get("status"),
     "ai_status":(public_state() or {}).get("status"),
     "ai_path_informed":(public_state() or {}).get("price_path_informed"),
     "five_minute":{k:five_min_monitor.state.get(k) for k in ("status","market","direction","target","forecast","reason")},
     "ai_performance":current.get("ai_performance"),
     "database_reporting_error":bool(current.get("database_error")),
     "numeric_accuracy_status":prices.get("status"),
     "numeric_accuracy_results":prices.get("results"),
     "paired_accuracy_comparisons":prices.get("paired_comparisons"),
     "shadow_paired_comparisons":prices.get("shadow_paired_comparisons"),
      "paired_direction_comparisons":prices.get("paired_direction_comparisons"),
     "guarded_primary":state.get("guarded_primary"),
     "manual_entry_review":state.get("manual_entry_review"),
     "target_validation":state.get("target_validation"),
     "active_alert_codes":[a.get("code") for a in state.get("alerts",[]) if isinstance(a,dict)]
    } | diagnostic_extras(state,five_min_monitor.state,first_five_quick_report)
   summary=await asyncio.to_thread(gather)
   print("BTC_READ_ONLY_DIAG "+json.dumps(summary,default=str,separators=(",",":")),flush=True)
  except Exception as exc:
   print("BTC_READ_ONLY_DIAG_ERROR "+type(exc).__name__,flush=True)
  await asyncio.sleep(600)

@asynccontextmanager
async def lifespan(app):
 task=asyncio.create_task(loop())
 context_task=asyncio.create_task(intelligence_loop())
 chart_task=asyncio.create_task(official_chart_sampler(state))
 async def five_min_worker():
  async with httpx.AsyncClient(headers={"User-Agent":"BTCResearchMonitor/1.0"}) as client:
   await five_min_monitor.loop(client,execute,verified_winner,state,postgres=bool(DB))
 five_task=asyncio.create_task(five_min_worker())
 # Paper-trading worker disabled: prediction-only mode.
 async def historical_job():
  try:
   historical_state.update(status="running",started=datetime.now(timezone.utc).isoformat())
   await collect_15m()
   records=json.loads(Path("historical_15m_7d.json").read_text())
   historical_state.update(status="complete",checked=len(records),targets=sum(x["price_to_beat"] is not None for x in records),winners=sum(x["winner"] is not None for x in records),verified=sum(x["status"]=="VERIFIED" for x in records),movement_complete=sum(x.get("movement_status")=="COMPLETE" for x in records),target_movement_complete=sum(x.get("movement_status")=="COMPLETE" and x.get("price_to_beat") is not None for x in records),finished=datetime.now(timezone.utc).isoformat())
  except Exception as exc: historical_state.update(status="error",error=str(exc)[:300])
 history_task=asyncio.create_task(historical_job())
 diagnostics_task=asyncio.create_task(read_only_diagnostics_loop())
 yield
 task.cancel()
 history_task.cancel()
 diagnostics_task.cancel()
 context_task.cancel()
 chart_task.cancel()
 five_task.cancel()
 pending_predictions=list(_prediction_tasks)
 for pending in pending_predictions:pending.cancel()
 for running in (task,history_task,context_task,chart_task,diagnostics_task,five_task,*pending_predictions):
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
  (await asyncio.to_thread(paper.control, connect,bool(DB),action))
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

def numeric_price_forecast_report():
 """Observed USD forecast errors against official market settlement metadata."""
 rows=iter_query_rows("""SELECT checkpoint,snapshot_json,calls_json,final_reference_mean,winner
   FROM research_calls WHERE final_reference_mean IS NOT NULL
   AND final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
   ORDER BY created_at DESC LIMIT 10000""")
 groups={}
 paired={}
 shadow_paired={}
 direction_paired={}
 for row in rows:
  if len(row)==5:
   checkpoint,snapshot_json,calls_json,actual,winner=row
  elif len(row)==4:
   checkpoint,snapshot_json,calls_json,actual=row
   winner=None
  else:continue
  try:
   snap=json.loads(snapshot_json);calls=json.loads(calls_json)
   if not isinstance(snap,dict) or not isinstance(calls,dict):continue
   y=number(actual)
   if y is None or y<=0:continue
   candidates={"CURRENT_BRTI_BASELINE":(snap.get("settlement_forecast") or {}).get("current_reference_usd"),
               "GUARDED_PRIMARY":(calls.get("GUARDED_PRIMARY") or {}).get("forecast_final_mean_usd"),
               "PRICE_PATH":(calls.get("PRICE_PATH") or {}).get("forecast_final_mean_usd"),
               "ANCHORED_SHADOW":(calls.get("ANCHORED_SHADOW") or {}).get("forecast_final_mean_usd"),
               "AI":(calls.get("AI") or {}).get("forecast_final_mean_usd") if (calls.get("AI") or {}).get("source")=="AI_REVIEW" else None}
   valid={k:number(v) for k,v in candidates.items()}
   # Compare direction on the same officially settled round and checkpoint.
   ai_call=calls.get("AI") or {}
   path_call=calls.get("PRICE_PATH") or {}
   ai_dir=ai_call.get("direction") if ai_call.get("source")=="AI_REVIEW" else None
   path_dir=path_call.get("direction")
   if (winner in ("UP","DOWN") and ai_dir in ("UP","DOWN")
       and path_dir in ("UP","DOWN")
       and valid.get("AI") is not None and valid.get("PRICE_PATH") is not None):
    dp=direction_paired.setdefault(checkpoint,{"checkpoint":checkpoint,
       "paired_rounds":0,"ai_wins":0,"path_wins":0,
       "both_correct":0,"ai_only_correct":0,"path_only_correct":0,"both_wrong":0})
    dp["paired_rounds"]+=1
    a_win=ai_dir==winner
    p_win=path_dir==winner
    dp["ai_wins"]+=int(a_win)
    dp["path_wins"]+=int(p_win)
    dp["both_correct"]+=int(a_win and p_win)
    dp["ai_only_correct"]+=int(a_win and not p_win)
    dp["path_only_correct"]+=int(p_win and not a_win)
    dp["both_wrong"]+=int(not a_win and not p_win)
   # Always compare against the exact same checkpoint/round; AI can be
   # missing on difficult rounds, so unpaired MAEs alone are misleading.
   if all(valid.get(k) is not None and valid[k]>0 for k in ("AI","PRICE_PATH","CURRENT_BRTI_BASELINE")):
    group=paired.setdefault(checkpoint,{"checkpoint":checkpoint,"paired_rounds":0,"sum_ai_error":0.0,"sum_path_error":0.0,"sum_static_error":0.0,"ai_better_than_static":0,"path_better_than_static":0})
    aerr=abs(valid["AI"]-y);perr=abs(valid["PRICE_PATH"]-y);serr=abs(valid["CURRENT_BRTI_BASELINE"]-y)
    group["paired_rounds"]+=1
    group["sum_ai_error"]+=aerr;group["sum_path_error"]+=perr;group["sum_static_error"]+=serr
    group["ai_better_than_static"]+=int(aerr<serr)
    group["path_better_than_static"]+=int(perr<serr)
   if all(valid.get(k) is not None and valid[k]>0 for k in ("AI","PRICE_PATH","CURRENT_BRTI_BASELINE","ANCHORED_SHADOW")):
    sg=shadow_paired.setdefault(checkpoint,{"checkpoint":checkpoint,"paired_rounds":0,
        "sum_shadow_error":0.0,"sum_ai_error":0.0,"sum_path_error":0.0,
        "shadow_better_than_path":0,"shadow_better_than_ai":0})
    serr=abs(valid["ANCHORED_SHADOW"]-y)
    aerr=abs(valid["AI"]-y)
    perr=abs(valid["PRICE_PATH"]-y)
    sg["paired_rounds"]+=1
    sg["sum_shadow_error"]+=serr
    sg["sum_ai_error"]+=aerr
    sg["sum_path_error"]+=perr
    sg["shadow_better_than_path"]+=int(serr<perr)
    sg["shadow_better_than_ai"]+=int(serr<aerr)
   for name,pred in candidates.items():
    p=valid[name]
    if p is None or p<=0:continue
    bucket=groups.setdefault((checkpoint,name),{"checkpoint":checkpoint,"source":name,"graded":0,"sum_abs_error_usd":0.0,"sum_square_error_usd":0.0})
    err=p-y
    bucket["graded"]+=1
    bucket["sum_abs_error_usd"]+=abs(err)
    bucket["sum_square_error_usd"]+=err*err
  except (ValueError,TypeError):continue
 out=[]
 for item in groups.values():
  n=item["graded"]
  out.append({"checkpoint":item["checkpoint"],"source":item["source"],"graded":n,
   "mean_absolute_error_usd":round(item["sum_abs_error_usd"]/n,2),
   "root_mean_square_error_usd":round((item["sum_square_error_usd"]/n)**.5,2)})
 paired_summary=[]
 for group in paired.values():
  n=group["paired_rounds"]
  paired_summary.append({"checkpoint":group["checkpoint"],"paired_rounds":n,
   "ai_mae_usd":round(group["sum_ai_error"]/n,2),
   "price_path_mae_usd":round(group["sum_path_error"]/n,2),
   "unchanged_reference_mae_usd":round(group["sum_static_error"]/n,2),
   "ai_better_than_unchanged_count":group["ai_better_than_static"],
   "path_better_than_unchanged_count":group["path_better_than_static"]})
 shadow_paired_summary=[]
 for sg in shadow_paired.values():
  n=sg["paired_rounds"]
  shadow_paired_summary.append({"checkpoint":sg["checkpoint"],"paired_rounds":n,
   "shadow_mae_usd":round(sg["sum_shadow_error"]/n,2),
   "ai_mae_usd":round(sg["sum_ai_error"]/n,2),
   "price_path_mae_usd":round(sg["sum_path_error"]/n,2),
   "shadow_better_than_path_count":sg["shadow_better_than_path"],
   "shadow_better_than_ai_count":sg["shadow_better_than_ai"]})
 directional=[]
 for dp in direction_paired.values():
  n=dp["paired_rounds"]
  directional.append({**dp,"ai_accuracy_pct":round(dp["ai_wins"]*100/n,1),
   "path_accuracy_pct":round(dp["path_wins"]*100/n,1)})
 return {"version":"forward-binary-and-numeric-audit-v4",
         "status":"observed" if out else "awaiting_official_settlement_prices",
         "results":out,"paired_comparisons":paired_summary,"shadow_paired_comparisons":shadow_paired_summary,"paired_direction_comparisons":directional,"definition":"Error against the authoritative Polymarket US BTC market assetPriceTerms.settlementPrice (official BRTI index). Public chart reconstructions and legacy unverified prices are excluded. Current BRTI at prediction time is a naive baseline; these are not calibrated win probabilities.",
         "note":"Paired numeric comparisons use the same checkpoint and officially settled round; paired direction also requires a published binary winner. Versions may be mixed; do not treat these historical scores as proof of a future edge. Errors are USD, not trade P&L."}

@app.get("/api/guarded-performance")
def guarded_performance():
 """Versioned prospective comparisons only; historical AI outcomes unchanged."""
 try:
  rows=iter_query_rows("""SELECT market,checkpoint,created_at,snapshot_json,calls_json,winner,
      CASE WHEN final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
      THEN final_reference_mean ELSE NULL END
      FROM research_calls ORDER BY created_at DESC LIMIT 10000""")
  return evaluate_guarded(rows)
 except Exception:return JSONResponse({"status":"unavailable","reason":"Guarded audit database unavailable"},status_code=503)

@app.get("/api/price-forecast/accuracy")
def price_forecast_accuracy():
 try:return numeric_price_forecast_report()
 except Exception:return JSONResponse({"status":"unavailable","error":"Price accuracy database unavailable"},status_code=503)

@app.get("/api/forecast/diagnostics")
def forecast_diagnostics():
 try:
  rows=iter_query_rows("""SELECT market,checkpoint,created_at,snapshot_json,calls_json,winner,
      CASE WHEN final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
       THEN final_reference_mean ELSE NULL END
      FROM research_calls ORDER BY created_at DESC LIMIT 5000""")
  return summarize_forecast_audit(rows)
 except Exception:return JSONResponse({"status":"unavailable","error":"Forecast audit database unavailable"},status_code=503)

@app.get("/api/forecast/training-readiness")
def forecast_training_readiness():
 try:
  rows=iter_query_rows("""SELECT market,checkpoint,created_at,snapshot_json,calls_json,winner,final_reference_mean
       FROM research_calls WHERE winner IN ('UP','DOWN') AND final_reference_mean IS NOT NULL
       AND final_reference_source='OFFICIAL_MARKET_SETTLEMENT'
       ORDER BY created_at DESC LIMIT 5000""")
  return score_training_readiness(rows)
 except Exception:return JSONResponse({"status":"unavailable","error":"Training evidence unavailable"},status_code=503)

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
@app.get("/api/market-chart/tip")
def market_chart_tip():
 """Tiny read-only official index changes for 1s chart refresh; never touches SQL."""
 now=time.time()
 if not eligible_tick_market(state,now):
  return {"status":"unavailable","market":state.get("market"),"points":[],
          "reason":"Verified BRTI round heartbeat unavailable","server_epoch":now}
 return {"market":state.get("market"),**latest_tip(now)}

@app.get("/api/market-chart/history")
def market_chart_history(range: str = Query(default="round")):
 """Bounded read-only BRTI ticks and historical exchange observations, no extra provider polls."""
 if range not in ("round","30s","1m","2m","5m","30m","60m"):
  return JSONResponse({"status":"invalid_range","reason":"Unsupported chart window"},status_code=400)
 current=state.get("market")
 ref=state.get("reference_price") or {}
 start=number(ref.get("window_start"));end=number(ref.get("window_end"))
 if not current or "btc-updown-15m-" not in current or start is None or end is None or end-start!=900:
  return {"status":"unavailable","reason":"No verified current 15-minute BRTI round","brti_points":[],"venue_series":[]}
 return chart_history_snapshot(range,round_start=start,round_end=end,market=current)

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

@app.get("/api/live")
def lightweight_live():
 """Read-only in-memory live feed independent of historical SQL."""
 return {"status":state.get("status"),"updated":state.get("updated"),
         "market":state.get("market"),"signal":state.get("signal"),
         "error":state.get("error"),"external":state.get("external") or {},
         "brti_chart":state.get("brti_chart"),"reference_price":state.get("reference_price"),
         "target_behavior":state.get("target_behavior"),"settlement_forecast":state.get("settlement_forecast"),"quick_forecast":state.get("quick_forecast"),
         "guarded_primary":state.get("guarded_primary"),"last_guarded_primary":state.get("last_guarded_primary"),
         "manual_entry_review":state.get("manual_entry_review"),"reversal_evidence":state.get("reversal_evidence"),
         "final_15m":state.get("final_15m"),"last_halfway_ai":state.get("last_halfway_ai"),
         "last_checkpoint":state.get("last_checkpoint"),
         "ai_confirmation":public_state(),"settlement_task_running":state.get("settlement_task_running",False),"settlement_task_error":state.get("settlement_task_error"),
         "server_epoch":time.time()}

_round_tiles_cache={"at":0.0,"data":None,"execute_id":None,"write_version":None}
_round_tiles_lock=_HistoryLock()
@app.get("/api/round-tiles")
def round_tiles_report():
 """Official-result display cache: avoid polling identical settled rounds every 10s."""
 try:
  now=time.monotonic()
  with _round_tiles_lock:
   if _round_tiles_cache["data"] is None or _round_tiles_cache["execute_id"]!=id(execute) or _round_tiles_cache["write_version"]!=_history_write_version or now-_round_tiles_cache["at"]>30:
    _round_tiles_cache.update(data=recent_round_tiles(execute),at=now,execute_id=id(execute),write_version=_history_write_version)
   return _round_tiles_cache["data"]
 except Exception:
  return JSONResponse({"status":"unavailable","reason":"Verified round history database unavailable"},status_code=503)

@app.get("/api/five-minute")
def five_minute_report():
 """Serve the worker's published snapshot; HTTP polling never repeats SQL."""
 live=dict(five_min_monitor.state)
 return {"live":live,"accuracy":live.get("accuracy") or {"status":"collecting"}}

@app.get("/api/quick-accuracy")
def quick_accuracy_report():
 try:return first_five_quick_report()
 except Exception:return JSONResponse({"status":"unavailable",
   "error":"First-five-minute quick prediction database unavailable"},status_code=503)

# Cached read-only checkpoint comparison; never alters frozen predictions.
_checkpoint_display_cache={"updated":0,"data":None}

@app.get("/api/checkpoint-scoreboard")
def checkpoint_scoreboard_report():
 try:
  now=time.time()
  if _checkpoint_display_cache["data"] is not None and now-_checkpoint_display_cache["updated"]<45:
   return _checkpoint_display_cache["data"]
  rows=iter_query_rows("""SELECT market,checkpoint,calls_json,winner FROM research_calls
     WHERE winner IN ('UP','DOWN') ORDER BY created_at DESC LIMIT 1500""")
  data=build_scoreboard(rows,five_min_monitor.report(execute))
  data["updated_epoch"]=now
  _checkpoint_display_cache.update(updated=now,data=data)
  return data
 except Exception:
  return JSONResponse({"status":"unavailable","reason":"Verified checkpoint history unavailable"},status_code=503)

# The historical scoreboard does not change every second; cache SQL reads while
# keeping state/AI connection information refreshed on each dashboard request.
_status_cache={"at":0.0,"data":None,"execute_id":None,"write_version":None}
_status_cache_lock=_HistoryLock()

def _read_status_history():
 now=time.monotonic()
 with _status_cache_lock:
  if _status_cache["data"] is not None and _status_cache["execute_id"]==id(execute) and _status_cache["write_version"]==_history_write_version and now-_status_cache["at"]<12:
   return _status_cache["data"]
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
  data={"counts":dict(rows),"ai_decision_counts":verified_counts,"ai_verified_counts":verified_counts,"ai_performance":ai_performance,"ai_decision_history":[dict(zip(("market","phase","direction","confidence","source","reason","created_at","result"),r)) for r in ai_rows],"accuracy_by_quality":accuracy_report(),"history":[dict(zip(("market","phase","direction","flip","score","created_at","result","quality","quality_reason","decision_source","decision_reason","ai_confidence"),r)) for r in recent]}
  _status_cache.update(data=data,at=time.monotonic(),execute_id=id(execute),write_version=_history_write_version)
  return data

@app.get("/api/status")
def status():
 try:
  return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),
          **_read_status_history()}
 except Exception as e: return {**state,"ai_confirmation":public_state(),"ai_connection":auth_state(),"database_error":str(e),"history":[]}

@app.get("/",response_class=HTMLResponse)
def home():
 return Path(__file__).with_name("terminal.html").read_text(encoding="utf-8")




