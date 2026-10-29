"""In-memory, read-only observed Bitcoin price history for a lightweight chart.

Polymarket US official BRTI ticks and separately sampled BTC/USD exchange prices.
No fabricated interpolation, no USD/USDT conflation, no private/trade endpoints.
A process restart resets the rolling one-hour chart; model records are unaffected.
"""
import math
import time
from collections import deque
from threading import RLock

RANGES={"30s":30,"1m":60,"2m":120,"5m":300,"30m":1800,"60m":3600}
_lock=RLock()
_brti={}
_usd={}
_STALE=45

def _positive(v):
 try:
  x=float(v)
  return x if math.isfinite(x) and x>0 else None
 except (TypeError,ValueError,OverflowError):return None

def record_brti(points, source, now=None):
 """Accept genuine timestamped official index ticks only; prune beyond one hour."""
 if source!="Polymarket US BTC reference / BRTI":return
 now=time.time() if now is None else now
 with _lock:
  for p in points or ():
   if not isinstance(p,(list,tuple)) or len(p)<2:continue
   try:t=float(p[0])
   except (TypeError,ValueError,OverflowError):continue
   v=_positive(p[1])
   if v is not None and math.isfinite(t) and now-3600<=t<=now+2 and t.is_integer():
    _brti[int(t)]=round(v,2)
  for t in list(_brti):
   if t<now-3600 or t>now+2:del _brti[t]

def record_exchange(snapshot, now=None):
 """Append actually returned USD ticker prices at their observation receipt time."""
 now=time.time() if now is None else now
 if not isinstance(snapshot,dict) or snapshot.get("status") not in ("available","limited"):return
 with _lock:
  for row in snapshot.get("exchanges") or []:
   if not isinstance(row,dict) or row.get("quote")!="USD" or row.get("status")!="responding":continue
   name=row.get("source")
   if not isinstance(name,str) or len(name)>28:continue
   price=_positive(row.get("price"))
   stamp=_positive(row.get("fetched_epoch"))
   if price is None or stamp is None or not -5<=now-stamp<=20:continue
   points=_usd.setdefault(name,deque(maxlen=300))
   if not points or stamp>points[-1][0]:
    points.append((round(stamp,2),round(price,2)))
  for name,points in list(_usd.items()):
   while points and points[0][0]<now-3600:points.popleft()
   if not points:del _usd[name]

def _thin(points,cap):
 if len(points)<=cap:return points
 stride=max(1,math.ceil(len(points)/(cap-1)))
 sampled=points[::stride]
 if sampled[-1]!=points[-1]:sampled.append(points[-1])
 return sampled

def snapshot(range_id, round_start=None, round_end=None, market=None, now=None):
 """Current market target and projection are deliberately *not* returned here."""
 now=time.time() if now is None else now
 span=RANGES.get(range_id)
 start=(float(round_start) if range_id=="round" and round_start is not None else now-(span or 900))
 if not math.isfinite(start) or start>now:start=now-900
 with _lock:
  brti=sorted((t,v) for t,v in _brti.items() if start<=t<=now)
  venue={name:[list(p) for p in points if start<=p[0]<=now] for name,points in _usd.items()}
  last=max(_brti) if _brti else None
  venue_ages={k:round(now-points[-1][-2],1) if points else None for k,points in _usd.items()}
 latest_age=round(now-last,1) if last is not None else None
 coverage=round(now-brti[0][0],1) if brti else 0
 return {"status":"fresh" if latest_age is not None and 0<=latest_age<=15 and len(brti)>=2 else "stale" if brti else "collecting",
         "range":range_id if range_id in RANGES or range_id=="round" else "round",
         "market":market,"window_start":round(start,2),
         "window_end":round_end if range_id=="round" else now,
         "sampled_at":round(now,2),
         "brti_age_seconds":latest_age,"brti_coverage_seconds":coverage,
         "brti_points":[list(p) for p in _thin(brti,440)],
         "venue_series":[{"source":name,"quote":"USD","status":"fresh" if venue_ages[name] is not None and 0<=venue_ages[name]<=_STALE else "stale",
                          "sample_age_seconds":venue_ages[name],
                          "points":_thin(pts,240)} for name,pts in sorted(venue.items()) if pts],
         "source":"Polymarket US official BRTI index ticks + observed BTC/USD public venue spot price samples",
         "limitations":"Last-exchange-price receipt time is not guaranteed trade execution time. Gaps not interpolated. Prior rounds and 30/60m windows accumulate in memory only after server startup. Only official BRTI determines settlement."}
