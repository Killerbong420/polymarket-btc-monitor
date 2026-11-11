"""Persist independent chart-pattern lifecycles from sequential COMPLETED 1m bars.

Tracks the first observed warning and later structural confirmation, breakout,
measured follow-through, invalidation or expiry. A completed pattern is immutable.
A historical reversal must never rewrite a previously reached objective.

This is an exchange-price-pattern study, not an official BRTI settlement result,
a trading result, a calibrated probability, or an executable signal. No backfills.
"""
import hashlib
import math
import time

VERSION = "pattern-lifecycle-v1"
OPEN = ("FORMING", "CONFIRMED", "BREAKOUT")
DONE = ("COMPLETED", "INVALIDATED", "EXPIRED")
_ALLOWED = {"BULL_FLAG":1, "BEAR_FLAG":-1, "DOUBLE_TOP":-1, "DOUBLE_BOTTOM":1}
_BREAKS = {"BREAKOUT_WATCH", "NECKLINE_BREAK_WATCH", "RETEST_HELD"}


def setup_tables(execute):
    execute("""CREATE TABLE IF NOT EXISTS pattern_lifecycle (
      id TEXT PRIMARY KEY, market TEXT NOT NULL, source TEXT NOT NULL,
      pattern TEXT NOT NULL, anchor_epoch INTEGER NOT NULL, direction INTEGER NOT NULL,
      phase TEXT NOT NULL, initial_phase TEXT NOT NULL,
      first_seen_epoch REAL NOT NULL, last_bar_epoch INTEGER NOT NULL,
      confirmed_epoch INTEGER, breakout_epoch INTEGER, completed_epoch INTEGER,
      invalidated_epoch INTEGER, expired_epoch INTEGER,
      trigger_usd REAL NOT NULL, invalidation_usd REAL NOT NULL,
      objective_usd REAL NOT NULL, initial_fit_pct REAL,
      result TEXT NOT NULL DEFAULT 'PENDING')""")
    execute("CREATE INDEX IF NOT EXISTS pattern_lifecycle_recent ON pattern_lifecycle (first_seen_epoch DESC)")
    execute("CREATE INDEX IF NOT EXISTS pattern_lifecycle_open ON pattern_lifecycle (source,phase,last_bar_epoch)")


def _valid_num(value):
    if isinstance(value,bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _new_event(market, signal, bar_epoch, now):
    if not isinstance(signal,dict) or signal.get("status") != "watch":
        return None
    pattern = signal.get("pattern")
    if pattern not in _ALLOWED or signal.get("phase") == "INVALIDATED":
        return None
    source = signal.get("source")
    anchor = _valid_num(signal.get("anchor_epoch"))
    trigger = _valid_num(signal.get("trigger_usd"))
    invalid = _valid_num(signal.get("invalidation_usd"))
    fit = _valid_num(signal.get("pattern_match_score_pct"))
    direction = _ALLOWED[pattern]
    if (not isinstance(source,str) or source not in ("Coinbase Exchange BTC-USD","Kraken BTC/USD")
        or anchor is None or anchor != int(anchor) or anchor > bar_epoch
        or trigger is None or invalid is None or trigger<=0 or invalid<=0
        or direction*(trigger-invalid)<=0
        or fit is None or not 0<=fit<=95):
        return None
    # Only capture genuinely newly observed formations; not months-old replays.
    if bar_epoch - anchor > 12*60 or anchor > now:
        return None
    risk = abs(trigger-invalid)
    pole = _valid_num(signal.get("pole_usd"))
    move = max(0.75*risk, min(0.60*pole, 1.5*risk)) if pattern.endswith("_FLAG") and pole is not None else risk
    objective = trigger + direction*move
    if objective<=0:
        return None
    key = "|".join((market,source,pattern,str(int(anchor))))
    identity = hashlib.sha256(key.encode()).hexdigest()[:32]
    initial = "BREAKOUT" if signal.get("phase") in _BREAKS else "FORMING"
    return (identity,market,source,pattern,int(anchor),direction,initial,initial,now,int(bar_epoch),
            None,int(bar_epoch) if initial=="BREAKOUT" else None,None,None,None,
            trigger,invalid,objective,fit,"PENDING")


def _advance(row, bar_epoch, close):
    """One-bar monotonic state transition using only observed completed close."""
    (ident,phase,last_bar,first_seen,trigger,invalid,objective,direction,confirmed,breakout,completed,invalidated,expired) = row
    if phase not in OPEN or bar_epoch <= last_bar:
        return None
    # Strict continuity: never infer event ordering across missing candles.
    if bar_epoch != last_bar + 60:
        return ("EXPIRED","OBSERVATION_GAP",confirmed,breakout,completed,invalidated,bar_epoch)
    age = bar_epoch - first_seen
    if age > 30*60:
        return ("EXPIRED","TIMED_OUT",confirmed,breakout,completed,invalidated,bar_epoch)
    if direction*(close-invalid) <= 0:
        return ("INVALIDATED","INVALIDATION_LEVEL_CLOSED",confirmed,breakout,completed,bar_epoch,expired)
    if phase=="BREAKOUT":
        if direction*(close-objective)>=0:
            return ("COMPLETED","MEASURED_OBJECTIVE_REACHED",confirmed,breakout,bar_epoch,invalidated,expired)
        return ("BREAKOUT","PENDING",confirmed,breakout,completed,invalidated,expired)
    if direction*(close-trigger)>0:
        return ("BREAKOUT","PENDING",confirmed,bar_epoch,completed,invalidated,expired)
    if phase=="FORMING":
        return ("CONFIRMED","PENDING",bar_epoch,breakout,completed,invalidated,expired)
    return ("CONFIRMED","PENDING",confirmed,breakout,completed,invalidated,expired)


def _recent(execute, market):
    rows=execute("""SELECT id,market,source,pattern,anchor_epoch,phase,initial_phase,first_seen_epoch,
     last_bar_epoch,confirmed_epoch,breakout_epoch,completed_epoch,invalidated_epoch,
     expired_epoch,trigger_usd,invalidation_usd,objective_usd,initial_fit_pct,result
     FROM pattern_lifecycle ORDER BY first_seen_epoch DESC LIMIT 12""",fetch=True)
    columns=("id","market","source","pattern","anchor_epoch","phase","initial_phase","first_seen_epoch",
             "last_bar_epoch","confirmed_epoch","breakout_epoch","completed_epoch",
             "invalidated_epoch","expired_epoch","trigger_usd","invalidation_usd",
             "objective_usd","initial_fit_pct","result")
    recent=[dict(zip(columns,row)) for row in rows]
    # Bounded, resolved-only cohort. Do not count pending or expired setups as
    # wins or losses. Generated on the existing once-per-candle database pass,
    # never on the 1s browser timer or live BRTI reference sampler.
    closed=execute("""SELECT pattern,phase,COUNT(*) FROM (
      SELECT pattern,phase FROM pattern_lifecycle
      WHERE phase IN ('COMPLETED','INVALIDATED')
      ORDER BY first_seen_epoch DESC LIMIT 200
    ) AS outcomes GROUP BY pattern,phase""",fetch=True)
    results={name:{"pattern":name,"completed":0,"invalidated":0,"resolved":0,
                   "objective_rate_pct":None,"sample_sufficient":False}
             for name in _ALLOWED}
    for name,phase,count in closed:
        if name not in results or phase not in DONE[:2]:
            continue
        if phase=="COMPLETED":
            results[name]["completed"]+=int(count)
        else:
            results[name]["invalidated"]+=int(count)
    for result in results.values():
        n=result["completed"]+result["invalidated"]
        result["resolved"]=n
        # Avoid presenting a fragile rate from only a few observations.
        result["sample_sufficient"]=n>=10
        result["objective_rate_pct"]=round(100*result["completed"]/n,1) if n>=10 else None
    return {"status":"available" if recent else "collecting","version":VERSION,
            "market":market,"recent":recent,
            "follow_through":{"status":"observed" if any(x["resolved"] for x in results.values())
                              else "collecting","cohort_limit":200,
                              "by_pattern":[results[name] for name in _ALLOWED],
                              "definition":"Completed / (completed + invalidated) among the most recent 200 resolved formations. Pending and expired excluded; not prediction win rate or Polymarket settlement."},
            "note":"Forward-only, completed exchange candles. COMPLETED means price closed at the preset pattern objective after a prior breakout, not a verified Polymarket trade."}


def record_completed_bar(execute, market, signal, candle, now=None):
    """Persist once per valid 1m candle. No upstream requests and no predictions.

    execute(sql, params=(), fetch=False) is the app's existing DB adapter.
    """
    now=time.time() if now is None else float(now)
    if not isinstance(market,str) or "btc-updown-15m-" not in market:
        return {"status":"unavailable","reason":"verified_15m_market_required"}
    if not isinstance(signal,dict) or not isinstance(candle,(list,tuple)) or len(candle)<6:
        return {"status":"unavailable","reason":"fresh_exchange_candle_required"}
    try:
        bar_start=_valid_num(candle[0])
        low,high,op,close=(_valid_num(candle[i]) for i in (1,2,3,4))
        volume=_valid_num(candle[5])
        if (bar_start is None or bar_start!=int(bar_start)
            or any(v is None for v in (low,high,op,close,volume))
            or low<=0 or volume<0 or low>min(op,close) or high<max(op,close)):
            return {"status":"unavailable","reason":"invalid_candle"}
        bar_epoch=int(bar_start)+60
        if not 0<=now-bar_epoch<=105:
            return {"status":"unavailable","reason":"stale_completed_candle"}
        source=signal.get("source")
        if source not in ("Coinbase Exchange BTC-USD","Kraken BTC/USD"):
            return {"status":"unavailable","reason":"unknown_source"}
        # Close pending setups even if the detector has switched to a newer
        # pattern. Only same-source bars may alter a stored event.
        pending=execute("""SELECT id,phase,last_bar_epoch,first_seen_epoch,
            trigger_usd,invalidation_usd,objective_usd,direction,
            confirmed_epoch,breakout_epoch,completed_epoch,invalidated_epoch,expired_epoch
            FROM pattern_lifecycle
            WHERE source=? AND phase IN ('FORMING','CONFIRMED','BREAKOUT')
            ORDER BY first_seen_epoch DESC LIMIT 80""",(source,),fetch=True)
        for row in pending:
            changed=_advance(row,bar_epoch,close)
            if changed is None:
                continue
            phase,result,confirmed,breakout,completed,invalidated,expired=changed
            execute("""UPDATE pattern_lifecycle SET phase=?,result=?,last_bar_epoch=?,
              confirmed_epoch=?,breakout_epoch=?,completed_epoch=?,invalidated_epoch=?,expired_epoch=?
              WHERE id=? AND phase=? AND last_bar_epoch=?""",
              (phase,result,bar_epoch,confirmed,breakout,completed,invalidated,expired,
               row[0],row[1],row[2]))
        candidate=_new_event(market,signal,bar_epoch,now)
        if candidate:
            execute("""INSERT INTO pattern_lifecycle (
              id,market,source,pattern,anchor_epoch,direction,phase,initial_phase,
              first_seen_epoch,last_bar_epoch,confirmed_epoch,breakout_epoch,
              completed_epoch,invalidated_epoch,expired_epoch,
              trigger_usd,invalidation_usd,objective_usd,initial_fit_pct,result)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(id) DO NOTHING""", candidate)
        return _recent(execute,market)
    except (ValueError,TypeError,OverflowError):
        return {"status":"unavailable","reason":"invalid_candle"}
