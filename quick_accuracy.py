"""First-five-minute, forward-only audit of the 60-second BRTI quick call.

One prospectively captured quick forecast per minute during the first five
minutes of each verified 15m Polymarket US BTC round. A real BRTI tick at
exactly observed_epoch + 60 grades the prediction; no synthetic interpolation,
exchange spot proxy, unofficial winner, or retroactive "prediction."
"""
import math

SOURCE = "Polymarket US BTC reference / BRTI"
VERSION = "brti-quick-60s-first5-audit-v1"


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def make_entry(market, start, now, quick):
    """Return a valid immutable snapshot or None; never invent a missed minute."""
    if not isinstance(market, str) or "btc-updown-15m-" not in market:
        return None
    start, now = positive(start), positive(now)
    if start is None or now is None:
        return None
    elapsed = now-start
    if not 0 <= elapsed < 300:
        return None
    slot = int(elapsed//60)
    q = quick if isinstance(quick, dict) else {}
    if q.get("status") != "available" or q.get("source") != SOURCE:
        return None
    direction = q.get("direction")
    if direction not in ("UP", "DOWN", "WAIT"):
        return None
    raw_epoch = positive(q.get("observed_epoch"))
    start_price = positive(q.get("current_reference_usd"))
    projected = positive(q.get("forecast_price_60s_usd"))
    if raw_epoch is None or raw_epoch != int(raw_epoch):
        return None
    observed = int(raw_epoch)
    if (start_price is None or projected is None or observed < start
            or observed > now or now-observed > 12
            or not start <= observed < start+300):
        return None
    return {"id": market+":QUICK60:"+str(slot),
            "market": market, "minute_slot": slot,
            "captured_epoch": int(now), "observed_epoch": observed,
            "target_epoch": observed+60,
            "starting_reference": start_price, "forecast_reference": projected,
            "direction": direction, "quality": str(q.get("quality") or "UNRATED")[:32],
            "reversal_watch": int(bool(q.get("reversal_watch"))),
            "result": "NO_CALL" if direction == "WAIT" else "PENDING",
            "model_version": str(q.get("version") or "UNKNOWN")[:96]}


def actual_tick_at(reference, target_epoch, now):
    """Exact observed 1s BRTI reference tick. Missing means unverifiable."""
    ref = reference if isinstance(reference, dict) else {}
    if ref.get("status") not in ("fresh", "stale") or ref.get("source") != SOURCE:
        return None
    now = positive(now)
    if now is None or now < target_epoch:
        return None
    for item in ref.get("points") or []:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            continue
        try:
            raw_t = float(item[0])
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(raw_t) or raw_t != target_epoch:
            continue
        p = positive(item[1])
        if p is not None:
            return p
    return None


def grade(start_price, direction, observed_price):
    """Never force unchanged index prices into a win or loss."""
    first = positive(start_price)
    last = positive(observed_price)
    if first is None or last is None or direction not in ("UP", "DOWN"):
        return None
    if last == first:
        return "TIE"
    return "WIN" if (last > first) == (direction == "UP") else "LOSS"


def summarize(rows, pending=0, no_call=0, ties=0):
    """Rows: newest 100 settled (result, minute_slot); pending separate."""
    groups = {slot: {"minute": slot+1, "wins": 0, "losses": 0, "graded": 0,
                      "accuracy_pct": None} for slot in range(5)}
    wins = losses = 0
    for result, slot in rows:
        if result not in ("WIN", "LOSS") or slot not in groups:
            continue
        item = groups[slot]
        item["wins"] += int(result == "WIN")
        item["losses"] += int(result == "LOSS")
        wins += int(result == "WIN")
        losses += int(result == "LOSS")
    for item in groups.values():
        n = item["wins"]+item["losses"]
        item["graded"] = n
        item["accuracy_pct"] = round(100*item["wins"]/n, 1) if n else None
    n = wins+losses
    return {"status": "observed" if n else "collecting",
            "version": VERSION, "window_size": 100,
            "graded": n, "wins": wins, "losses": losses,
            "accuracy_pct": round(100*wins/n, 1) if n else None,
            "pending": pending, "no_call": no_call, "ties": ties,
            "minutes": list(groups.values()),
            "basis": "Latest 100 graded next-60-second BRTI quick calls captured during minutes 1-5 of the 15m market",
            "note": "One frozen forecast per market-minute, graded only against an exact observed BRTI tick 60 seconds after its input tick. Pending, no-calls and ties are excluded. This is not the 15-minute UP/DOWN settlement win rate or trade P&L."}
