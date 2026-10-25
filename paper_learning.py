"""Forward-only learning from fake-money Polymarket 15-minute opportunities.

Hypothetical top-of-book prices are not executable-depth fills or guaranteed profits.
"""
import os
import time
import uuid

MIN_MARKETS = 40
BANDS = (("LOW", .10, .35), ("MID", .35, .60), ("HIGH", .60, .85))

def band_for(price):
    return next((name for name, lo, hi in BANDS if lo <= price < hi), None)

def setup(execute):
    execute("""CREATE TABLE IF NOT EXISTS paper_learning_samples (
      id TEXT PRIMARY KEY, market TEXT NOT NULL, direction TEXT NOT NULL,
      price_band TEXT NOT NULL, observed_epoch REAL NOT NULL, entry_ask REAL NOT NULL,
      quantity INTEGER NOT NULL, entry_cost_cents INTEGER NOT NULL,
      deadline_epoch REAL NOT NULL, status TEXT NOT NULL,
      last_roi_pct REAL, best_roi_pct REAL, worst_roi_pct REAL,
      exit_epoch REAL, exit_roi_pct REAL,
      UNIQUE(market,direction,price_band))""")
    execute("CREATE INDEX IF NOT EXISTS learning_status_idx ON paper_learning_samples (market,status)")

def stats(execute, direction, band):
    # One earliest recorded observation per market+direction+band, not every price tick.
    rows = execute("SELECT market,status,exit_roi_pct FROM paper_learning_samples "
                   "WHERE direction=? AND price_band=? AND status IN ('TARGET','STOP','TIME') "
                   "ORDER BY observed_epoch DESC LIMIT 1000", (direction,band), fetch=True)
    outcomes = [(status, float(roi)) for _,status,roi in reversed(rows) if roi is not None]
    n = len(outcomes)
    target = sum(status == "TARGET" for status,roi in outcomes)
    rate = target/n if n else 0
    mean = sum(roi for status,roi in outcomes)/n if n else None
    old = outcomes[:n//2]
    new = outcomes[n//2:]
    def hit_rate(part):
        return sum(status == "TARGET" for status,roi in part)/len(part) if part else 0
    older, newer = hit_rate(old), hit_rate(new)
    qualified = n >= MIN_MARKETS and rate >= .68 and mean >= 1.5 and older >= .60 and newer >= .60
    return {"direction":direction,"price_band":band,"distinct_markets":n,
            "target_hits":target,"hit_rate_pct":round(rate*100,1),
            "average_modelled_return_pct":round(mean,2) if mean is not None else None,
            "older_hit_rate_pct":round(older*100,1),
            "recent_hit_rate_pct":round(newer*100,1),
            "eligible_for_bounded_relaxation":qualified}

def policy(execute, direction, price):
    band = band_for(price)
    if band is None or direction not in ("UP","DOWN"):
        return {"enabled":False,"reason":"Entry outside monitored bands"}
    outcome = stats(execute,direction,band)
    allowed = os.getenv("PAPER_LEARNING_ENABLED","true").lower() in ("1","yes","true")
    return {**outcome,"enabled":bool(allowed and outcome["eligible_for_bounded_relaxation"]),
            "paper_only":True}

def observe(execute, quote):
    """Observe subsequent quotes only. First hit of target, stop or deadline wins."""
    from paper_trader import RISK, cost_lots, exit_mark
    from research import market_start, number
    if quote.get("status") != "available":return
    market = str(quote.get("market") or "")
    start = market_start(market)
    if "btc-updown-15m-" not in market or start is None:return
    now = time.time()
    age = now - float(quote.get("fetched_epoch") or 0)
    if not 0 <= age <= 15 or now > start+915:return
    rows = execute("SELECT id,direction,observed_epoch,quantity,entry_cost_cents,deadline_epoch,"
                   "best_roi_pct,worst_roi_pct FROM paper_learning_samples "
                   "WHERE market=? AND status='WATCHING'", (market,), fetch=True)
    for ident,direction,opened,qty,cost,deadline,prior_best,prior_worst in rows:
        raw_bid = number(quote.get("bid" if direction=="UP" else "ask"))
        bid = 1-raw_bid if direction=="DOWN" and raw_bid is not None else raw_bid
        if bid is not None and 0 < bid < 1 and now > opened:
            net, missing = exit_mark({"bids":[(bid,qty)],"offers":[]},"UP",qty,RISK["slippage"])
            if missing == 0 and cost:
                roi = round(100*(net-cost)/cost,3)
                best = max(prior_best if prior_best is not None else roi,roi)
                worst = min(prior_worst if prior_worst is not None else roi,roi)
                result = ("TARGET" if roi >= 100*RISK["take_profit_pct"] else
                          "STOP" if roi <= -100*RISK["stop_loss_pct"] else
                          "TIME" if now >= deadline else "WATCHING")
                execute("UPDATE paper_learning_samples SET last_roi_pct=?,best_roi_pct=?,"
                        "worst_roi_pct=?,status=?,exit_epoch=?,exit_roi_pct=? WHERE id=?",
                        (roi,best,worst,result,
                         now if result!="WATCHING" else None,
                         roi if result!="WATCHING" else None,ident))
                continue
        if now >= deadline:
            execute("UPDATE paper_learning_samples SET status='UNOBSERVED',exit_epoch=? WHERE id=?",
                    (now,ident))
    expiry = start+900-RISK["exit_before_expiry_seconds"]
    spread = number(quote.get("spread"))
    if now >= expiry-30 or spread is None or spread > RISK["max_spread"]:return
    if execute("SELECT 1 FROM paper_positions WHERE market=? LIMIT 1",(market,),fetch=True):return
    for direction in ("UP","DOWN"):
        ask = number(quote.get("up_entry" if direction=="UP" else "down_entry"))
        if ask is None or band_for(ask) is None or ask+RISK["slippage"]>=RISK["max_price"]:continue
        qty = 10
        cost,_ = cost_lots([(ask+RISK["slippage"],qty)])
        if cost/(100*qty) >= RISK["max_all_in_price"]:continue
        execute("INSERT INTO paper_learning_samples "
                "(id,market,direction,price_band,observed_epoch,entry_ask,quantity,"
                "entry_cost_cents,deadline_epoch,status) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(market,direction,price_band) DO NOTHING",
                (str(uuid.uuid4()),market,direction,band_for(ask),now,ask,qty,cost,
                 min(now+RISK["max_hold_seconds"],expiry),"WATCHING"))

def report(execute):
    rows = execute("SELECT market,direction,price_band,observed_epoch,entry_ask,status,"
                   "exit_epoch,exit_roi_pct,best_roi_pct,worst_roi_pct "
                   "FROM paper_learning_samples WHERE status<>'WATCHING' "
                   "ORDER BY exit_epoch DESC LIMIT 30",fetch=True)
    counts = dict(execute("SELECT status,COUNT(*) FROM paper_learning_samples GROUP BY status",fetch=True))
    enabled = os.getenv("PAPER_LEARNING_ENABLED","true").lower() in ("1","yes","true")
    return {"mode":"FORWARD_PAPER_LEARNING","enabled":enabled,"real_orders_enabled":False,
            "minimum_independent_markets":MIN_MARKETS,"outcomes":counts,
            "segments":[stats(execute,d,b) for d in ("UP","DOWN") for b,_,_ in BANDS],
            "recent":[dict(zip(("market","direction","price_band","entry_epoch","entry_ask",
                                "status","exit_epoch","modelled_return_pct",
                                "best_observed_roi_pct","worst_observed_roi_pct"),r)) for r in rows],
            "note":"Forward-only 10-contract top-of-book hypothetical fills with estimated fees and slippage, NOT verified executable order-book depth. Counts stop-losses and timeouts, not just winners. At least 40 independent markets, positive average modelled return, and good results in two chronological cohorts are required before a small paper-only dip-filter relaxation. No real trading."}
