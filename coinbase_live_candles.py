"""Fail-closed Coinbase BTC-USD one-minute candles from the EXISTING trade WebSocket.

No extra sockets or REST requests. Coinbase's matches channel may drop trades,
so we check sequential trade IDs and heartbeat IDs. Never fill missing minutes,
bridge disconnects, or call a developing candle a completed observation.
The normal Coinbase REST candles can fill missing *verified* historical minutes.
"""
from collections import OrderedDict
import math
import time
from datetime import datetime

SOURCE="Coinbase Exchange BTC-USD"
MAX_MINUTES=70
HEARTBEAT_FRESH_SECONDS=8
CANDLE_CLOSE_DELAY_SECONDS=8


def _number(value):
    try:
        val=float(value)
        return val if math.isfinite(val) else None
    except (TypeError,ValueError,OverflowError):
        return None


class CoinbaseMinuteBuilder:
    def __init__(self):
        self.connected_at=None
        self.bars=OrderedDict()
        self.bad_minutes=set()
        self.last_trade_id=None
        self.last_trade_at=None
        self.heartbeat_at=None
        self.last_heartbeat_id=None
        self.failure_reason="websocket_not_connected"

    def connected(self,now):
        self.connected_at=float(now)
        self.bars.clear()
        self.bad_minutes.clear()
        self.last_trade_id=None
        self.last_trade_at=None
        self.heartbeat_at=None
        self.last_heartbeat_id=None
        self.failure_reason="warming_completed_candles"

    def disconnected(self,reason="websocket_disconnected"):
        # Never join observations across a reconnect.
        self.connected_at=None
        self.bars.clear()
        self.bad_minutes.clear()
        self.last_trade_id=None
        self.last_trade_at=None
        self.heartbeat_at=None
        self.last_heartbeat_id=None
        self.failure_reason=str(reason)

    def _mark_gap(self,a,b):
        if a is None or b is None:
            return
        lo=int(min(a,b))//60*60
        hi=int(max(a,b))//60*60
        for t in range(lo,min(hi,lo+MAX_MINUTES*60)+1,60):
            self.bad_minutes.add(t)
        self.failure_reason="trade_or_heartbeat_gap"

    def heartbeat(self,message,now=None):
        now=time.time() if now is None else float(now)
        if (self.connected_at is None or not isinstance(message,dict)
                or message.get("type")!="heartbeat"
                or message.get("product_id")!="BTC-USD"):
            return
        observed=_number(message.get("last_trade_id"))
        instant=_number(message.get("time_epoch"))
        if instant is None:
            try:instant=datetime.fromisoformat(str(message.get("time")).replace("Z","+00:00")).timestamp()
            except (TypeError,ValueError,OverflowError):instant=None
        if observed is None or observed!=int(observed) or instant is None or abs(now-instant)>12:
            self.failure_reason="invalid_heartbeat"
            self._mark_gap(self.heartbeat_at or now,now)
            return
        if self.heartbeat_at is not None and now-self.heartbeat_at>HEARTBEAT_FRESH_SECONDS:
            self._mark_gap(self.heartbeat_at,now)
        self.heartbeat_at=now
        trade_id=int(observed)
        if self.last_trade_id is None:
            self.last_trade_id=trade_id
        elif trade_id>self.last_trade_id:
            # Matches channel can lose messages. A heartbeat ahead of the
            # highest observed match is independent evidence of missed trades.
            self._mark_gap(self.last_trade_at or self.connected_at,instant)
            self.last_trade_id=trade_id
        self.last_heartbeat_id=trade_id

    def append(self,event,now=None):
        now=time.time() if now is None else float(now)
        if self.connected_at is None or self.heartbeat_at is None:
            return False
        try:
            ts,price,size,side,ident=event
            ts,price,size=_number(ts),_number(price),_number(size)
            ident=int(ident)
            if (ts is None or price is None or size is None or
                ts<now-15 or ts>now+2 or price<=0 or size<=0 or
                (self.last_trade_id is not None and ident<=self.last_trade_id)):
                return False
        except (ValueError,TypeError,OverflowError):
            return False
        minute=int(ts)//60*60
        if self.last_trade_id is not None and ident!=self.last_trade_id+1:
            self._mark_gap(self.last_trade_at or self.connected_at,ts)
        if self.last_trade_at is not None and ts<self.last_trade_at-2:
            self._mark_gap(self.last_trade_at,ts)
        self.last_trade_id=ident
        self.last_trade_at=ts
        current=self.bars.get(minute)
        if current is None:
            current=[minute,price,price,price,price,0.0]
            self.bars[minute]=current
        else:
            # Coinbase match timestamps, not arrival order, own candle order.
            current[1]=min(current[1],price)
            current[2]=max(current[2],price)
            current[4]=price
        current[5]+=size
        while len(self.bars)>MAX_MINUTES:
            oldest,_=self.bars.popitem(last=False)
            self.bad_minutes.discard(oldest)
        return True

    def snapshot(self,now=None):
        now=time.time() if now is None else float(now)
        if self.connected_at is None:
            return {"status":"unavailable","reason":self.failure_reason,"candles":[]}
        if self.heartbeat_at is None or not 0<=now-self.heartbeat_at<=HEARTBEAT_FRESH_SECONDS:
            return {"status":"unavailable","reason":"coinbase_heartbeat_stale","candles":[]}
        # Only complete minutes; a safety delay permits late match delivery.
        completed=[]
        for start,bar in self.bars.items():
            if now<start+60+CANDLE_CLOSE_DELAY_SECONDS:
                continue
            if self.connected_at>start or start in self.bad_minutes:
                continue
            if not all(math.isfinite(float(x)) for x in bar):
                continue
            completed.append(list(bar))
        if not completed:
            return {"status":"warming","reason":self.failure_reason,"candles":[]}
        return {"status":"available","source":SOURCE,"transport":"websocket_matches_heartbeat",
                "candles":completed[-MAX_MINUTES:],
                "last_completed_epoch":completed[-1][0]+60,
                "heartbeat_age_seconds":round(now-self.heartbeat_at,2),
                "last_trade_epoch":self.last_trade_at,
                "no_missing_trades_assumed":True}


def compose_coinbase_candles(rest_candles,stream,now=None):
    """Join only actual, timestamped Coinbase observations into a contiguous tail.

    REST is authoritative on overlapping minute candles (not every WebSocket
    minute must match exactly if some matches were delayed). WebSocket bars
    enter ONLY when heartbeat/trade-ID coverage was sufficient.
    """
    now=time.time() if now is None else float(now)
    bars={}
    provenance={}
    for source,items in (("rest",rest_candles or []),
                         ("websocket",stream.get("candles",[]) if stream.get("status")=="available" else [])):
        for candle in items[-MAX_MINUTES:]:
            try:
                if not isinstance(candle,(tuple,list)) or len(candle)<6:continue
                ts=_number(candle[0])
                values=[_number(v) for v in candle[1:6]]
                if (ts is None or ts!=int(ts) or int(ts)%60 or
                    any(v is None for v in values)):
                    continue
                low,high,op,close,volume=values
                if low<=0 or low>min(op,close) or high<max(op,close) or volume<=0:
                    continue
                if ts+60>now:
                    continue
                start=int(ts)
                # Do not override genuine REST bars with inferred trade candles.
                if start in bars and source=="websocket":continue
                bars[start]=[start,low,high,op,close,volume]
                provenance[start]=source
            except (ValueError,TypeError,OverflowError):
                continue
    if not bars:
        return {"status":"unavailable","reason":"no_verified_coinbase_candles",
                "candles":[],"transport":"unavailable"}
    last=max(bars)
    suffix=[bars[last]]
    t=last-60
    while t in bars and len(suffix)<MAX_MINUTES:
        suffix.append(bars[t])
        t-=60
    suffix.reverse()
    age=now-(last+60)
    origin=provenance[last]
    result={"status":"available" if 0<=age<=75 and len(suffix)>=35 else "unavailable",
            "reason":("current" if 0<=age<=75 and len(suffix)>=35 else
                      "coinbase_completed_candle_stale" if age>75 else
                      "need_35_contiguous_coinbase_candles"),
            "source":SOURCE,"transport":("websocket_verified" if origin=="websocket" else "rest_verified"),
            "last_completed_epoch":last+60,"candle_age_seconds":round(age,1),
            "verified_minutes":len(suffix),
            "websocket_minutes":sum(provenance[b[0]]=="websocket" for b in suffix),
            "candles":suffix}
    return result
