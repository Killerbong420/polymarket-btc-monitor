"""Prospective, read-only 15m BRTI minute research and source-integrity checks.

Take one immutable sample per real market minute using already-fetched official
BRTI/Polymarket US inputs. No extra upstream requests, no orders, no model
promotion. The later published official winner is joined only for evaluation.
"""
import json
import math

from brti_volatility_shadow import brti_volatility_shadow, VERSION as SHADOW_VERSION
from research import market_start

VERSION = "official-brti-minute-research-v1"
SOURCE = "Polymarket US BTC reference / BRTI"


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, OverflowError):
        return None


def book_freshness(quote, market, reference, now):
    """Fresh top-of-book observations are not necessarily executable depth.

    Detect stale quotes and label rapid reference moves for recheck; do NOT
    infer that a changing BTC index proves an unchanged contract book is stale.
    """
    base = {"status": "unavailable", "orders_allowed": False,
            "source": "Polymarket US BBO", "rapid_move_is_quote_staleness": False}
    if not isinstance(quote, dict) or quote.get("status") != "available" or quote.get("market") != market:
        return {**base, "reason": "unverified_matching_quote"}
    ts, now = finite(quote.get("fetched_epoch")), finite(now)
    bid, ask = finite(quote.get("bid")), finite(quote.get("ask"))
    if (ts is None or now is None or ts > now + 2
            or (bid is None and ask is None)
            or (bid is not None and not 0 <= bid <= 1)
            or (ask is not None and not 0 <= ask <= 1)
            or (bid is not None and ask is not None and bid > ask)):
        return {**base, "reason": "invalid_quote_or_timestamp"}
    age = max(0.0, now-ts)
    if age > 10:
        return {**base, "status": "stale", "age_seconds": round(age, 2),
                "reason": "quote_older_than_ten_seconds"}
    points = []
    if isinstance(reference, dict) and reference.get("status") == "fresh" and reference.get("source") == SOURCE:
        # Quote-risk warnings must not use a point newer than the last
        # official observation declared in the same reference snapshot.
        latest_official_epoch = finite(reference.get("price_epoch"))
        seen = {}
        for item in reference.get("points") or []:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            t, price = finite(item[0]), finite(item[1])
            if (t is not None and price is not None and price > 0
                    and latest_official_epoch is not None
                    and t <= min(now+1, latest_official_epoch)):
                seen[int(t)] = price
        points = sorted(seen.items())
    move_bps = None
    if points:
        latest, price = points[-1]
        prior = [(t, p) for t,p in points if latest-15 <= t <= latest-8]
        if prior:
            move_bps = (price/prior[-1][1]-1)*10000
    rapid = move_bps is not None and abs(move_bps) >= 5.0
    return {**base, "status": "rapid_move_recheck" if rapid else "fresh",
            "reason": "recent_brti_jump_not_proven_stale_quote" if rapid else "fresh_quote_snapshot",
            "age_seconds": round(age, 2),
            "observed_8_to_15s_brti_move_bps": round(move_bps, 3) if move_bps is not None else None,
            "note": "BBO fetched time is known; unchanged book timestamps or true executable liquidity cannot be inferred from this snapshot."}


def minute_observation(market, seconds_left, official_target, reference,
                       behavior, path, quote, now, validation=None):
    """Produce a frozen feature vector, rejecting stale/spot inputs.

    A parity failure is kept visible for diagnostics and cannot contribute
    a theoretical probability to official-settlement scoring.
    """
    base = {"status": "unavailable", "version": VERSION, "research_only": True,
            "no_orders": True, "market": market}
    if not isinstance(market, str) or "btc-updown-15m-" not in market:
        return {**base, "reason": "unverified_15m_market"}
    start = market_start(market)
    now, left, target = finite(now), finite(seconds_left), finite(official_target)
    if start is None or now is None or left is None or target is None or target <= 0:
        return {**base, "reason": "invalid_clock_or_official_target"}
    elapsed = now - start
    if not 0 <= elapsed < 900 or not 0 < left <= 900 or abs((900-elapsed)-left)>20:
        return {**base, "reason": "market_clock_mismatch"}
    ref = reference if isinstance(reference, dict) else {}
    beh = behavior if isinstance(behavior, dict) else {}
    path = path if isinstance(path, dict) else {}
    if ref.get("source") != SOURCE or ref.get("status") != "fresh":
        return {**base, "reason": "verified_fresh_brti_required"}
    age, tick, price = (finite(ref.get(k)) for k in ("age_seconds", "price_epoch", "price"))
    if (None in (age, tick, price) or price <= 0 or age < 0 or age > 15
            or tick > now + 2 or now-tick > 15):
        return {**base, "reason": "stale_or_incoherent_brti_tick"}
    slot = int(elapsed // 60)
    computed_gap = 100*(price/target-1)
    reported = finite(beh.get("target_gap_pct"))
    elapsed_behavior = finite(beh.get("elapsed_seconds"))
    matches = (
        beh.get("source") == SOURCE and beh.get("status") == "available"
        and beh.get("is_reference") is True
        and reported is not None and abs(reported-computed_gap) <= .001
        and elapsed_behavior is not None and abs(elapsed_behavior-elapsed) <= 20
        and (not isinstance(validation, dict) or validation.get("material") is not True)
    )
    path_price = finite(path.get("current_reference_usd"))
    path_target = finite(path.get("target_usd"))
    path_consistent = (path.get("status") == "available" and
                       path_price is not None and abs(path_price-price) <= .02 and
                       path_target is not None and abs(path_target-target) <= .02)
    evidence_missing = (
        beh.get("status") != "available" or beh.get("is_reference") is not True
        or path.get("status") != "available" or reported is None
        or path_price is None or path_target is None
    )
    parity = "UNAVAILABLE" if evidence_missing else ("MATCH" if matches and path_consistent else "MISMATCH")
    shadow = brti_volatility_shadow(ref, path, target, left, validation) if parity == "MATCH" else {}
    return {**base, "status": "captured", "market_minute": slot+1,
            "minute_slot": slot, "observed_epoch": round(now, 3),
            "seconds_left": round(left, 2), "official_target": round(target, 2),
            "reference_price": round(price, 2), "reference_epoch": int(tick),
            "reference_age_seconds": round(age, 2),
            "gap_pct_from_official": round(computed_gap, 6),
            "gap_pct_from_live": reported,
            "input_parity": parity,
            "input_parity_details": {
                "behavior_matches_official_gap": matches,
                "forecast_uses_same_target_and_index": path_consistent
            },
            "book": book_freshness(quote, market, ref, now),
            "shadow_source": SHADOW_VERSION,
            "theoretical_up_probability": (
                shadow.get("model_up_probability") if shadow.get("status") == "available" else None),
            "theoretical_probability_calibrated": False,
            "note": "One forward-captured point per market minute, no hindsight. Official settlement only grades after resolution."}


def replay_input_parity(sample):
    """Recalculate immutable live price/gap/clock features from frozen inputs.

    This catches post-capture corruption or train/serve feature skew without
    downloading any late prices or using the eventual outcome.
    """
    if not isinstance(sample, dict) or sample.get("input_parity") != "MATCH":
        return False
    start = market_start(sample.get("market"))
    target = finite(sample.get("official_target"))
    price = finite(sample.get("reference_price"))
    tick = finite(sample.get("reference_epoch"))
    now = finite(sample.get("observed_epoch"))
    age = finite(sample.get("reference_age_seconds"))
    remaining = finite(sample.get("seconds_left"))
    gap = finite(sample.get("gap_pct_from_official"))
    live_gap = finite(sample.get("gap_pct_from_live"))
    if (start is None or None in (target, price, tick, now, age, remaining, gap, live_gap)
            or price <= 0 or target <= 0 or not 0 <= age <= 15 or
            tick > now + 2 or now-tick > 15 or
            not 0 < remaining <= 900 or abs((900-(now-start))-remaining) > 20
            or not 0 <= now-start < 900):
        return False
    if sample.get("minute_slot") != int((now-start)//60):
        return False
    computed = 100*(price/target-1)
    return abs(computed-gap) <= .001 and abs(computed-live_gap) <= .001


def evaluate_minutes(rows):
    """Rows: frozen market, slot, JSON, later independently verified winner.

    One sample per market per slot. Count parity errors separately; evaluate
    Brier and coarse reliability bins only on valid, prospective probabilities.
    """
    groups = {}
    seen = set()
    for market, slot, raw, winner in rows:
        if winner not in ("UP", "DOWN") or not isinstance(slot, int) or not 0 <= slot < 15:
            continue
        key = (market,slot)
        if key in seen:
            continue
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(s, dict) or s.get("version") != VERSION or s.get("market") != market or s.get("minute_slot") != slot:
            continue
        seen.add(key)
        g = groups.setdefault(slot, {"minute":slot+1, "officially_settled_samples":0,
                                     "parity_mismatches":0,"input_unavailable":0,
                                     "replay_mismatches":0,"missing_or_unrated":0,
                                     "graded":0, "correct":0, "sum_brier":0.0,
                                     "up_wins":0,"bins":{},"book_statuses":{}})
        g["officially_settled_samples"] += 1
        book = s.get("book") or {}
        book_state = str(book.get("status") or "unknown") if isinstance(book,dict) else "unknown"
        book_state = book_state if book_state in ("fresh","rapid_move_recheck","stale","unavailable") else "unknown"
        g["book_statuses"][book_state] = g["book_statuses"].get(book_state,0)+1
        if s.get("input_parity") == "UNAVAILABLE":
            g["input_unavailable"] += 1
            continue
        if s.get("input_parity") != "MATCH":
            g["parity_mismatches"] += 1
            continue
        if not replay_input_parity(s):
            g["replay_mismatches"] += 1
            continue
        p = finite(s.get("theoretical_up_probability"))
        if p is None or not 0 < p < 1 or s.get("shadow_source") != SHADOW_VERSION:
            g["missing_or_unrated"] += 1
            continue
        y = int(winner == "UP")
        g["graded"] += 1
        g["up_wins"] += y
        g["correct"] += int((p>.5 and y==1) or (p<.5 and y==0))
        g["sum_brier"] += (p-y)**2
        bucket = min(4, int(p*5))
        b = g["bins"].setdefault(bucket, {"count":0,"sum_forecast":0.0,"up_wins":0})
        b["count"] += 1
        b["sum_forecast"] += p
        b["up_wins"] += y

    result=[]
    for slot,g in sorted(groups.items()):
        n = g["graded"]
        reliability = []
        for b,x in sorted(g["bins"].items()):
            count=x["count"]
            reliability.append({"probability_band":f"{b*20}-{(b+1)*20}%",
                                "samples":count,"mean_theoretical_up_pct":round(100*x["sum_forecast"]/count,1),
                                "observed_up_pct":round(100*x["up_wins"]/count,1),
                                "sufficient_for_review":count>=30})
        result.append({"market_minute":slot+1,"officially_settled_samples":g["officially_settled_samples"],
                       "input_parity_mismatches":g["parity_mismatches"],
                       "unavailable_inputs":g["input_unavailable"],
                       "archived_replay_mismatches":g["replay_mismatches"],
                       "book_statuses":g["book_statuses"],
                       "missing_or_unrated":g["missing_or_unrated"],"graded":n,
                       "theoretical_brier":round(g["sum_brier"]/n,5) if n else None,
                       "neutral_brier":.25,
                       "directional_accuracy_pct":round(100*g["correct"]/n,1) if n else None,
                       "observed_up_frequency_pct":round(100*g["up_wins"]/n,1) if n else None,
                       "reliability":reliability, "sample_sufficient_for_review":n>=100})
    return {"status":"observed_research_only" if result else "collecting",
            "version":VERSION, "auto_promote":False,"prediction_meter_unchanged":True,
            "minutes":result,
            "limitations":"Minute-cohort descriptive forward Brier and reliability only. One market contributes up to 15 dependent samples; do not pool them as independent rounds. Requires later OFFICIAL Polymarket US BRTI outcome. A neutral .25 reference is not a chronological validation baseline. No calibrated live odds or demonstrated edge."}
