"""Read-only, contemporaneous comparison of Polymarket US WS and REST BBO."""
import time

def compare_quotes(ws, rest, market, now=None, tolerance=0.03):
    now=time.time() if now is None else now
    if not ws or ws.get("market")!=market or not 0 <= now-ws.get("received_at", -1) <= 10:
        return {"status":"unavailable","reason":"ws_stale_or_missing","market":market}
    if not isinstance(rest,dict) or rest.get("status")!="available" or rest.get("market")!=market:
        return {"status":"unavailable","reason":"rest_stale_or_missing","market":market}
    if not 0 <= now-rest.get("fetched_epoch",-1) <= 10:
        return {"status":"unavailable","reason":"rest_stale_or_missing","market":market}
    if any(ws.get(k) is None or rest.get(k) is None for k in ("bid","ask")):
        return {"status":"unavailable","reason":"incomplete_book","market":market}
    bid=round(abs(ws["bid"]-rest["bid"]),6)
    ask=round(abs(ws["ask"]-rest["ask"]),6)
    return {"status":"match" if max(bid,ask)<=tolerance else "divergent",
            "market":market,"bid_difference":bid,"ask_difference":ask,
            "max_difference":max(bid,ask),"tolerance":tolerance,
            "ws_age_seconds":round(now-ws["received_at"],2),
            "rest_age_seconds":round(now-rest["fetched_epoch"],2)}
