"""Prospective, read-only 15m BRTI path-first research decisions.

No orders, no invented probabilities, no backfilled predictions. Raw AI calls
and the displayed historical AI scoreboard remain independent and immutable.
"""
import math

VERSION = "guarded-path-primary-v1"
INDEX_SOURCE = "Polymarket US BTC reference / BRTI"


def finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def clamp(value, lower, upper):
    return min(upper, max(lower, value))


def reversal_evidence(reference, seconds_left):
    """Observed short/long BRTI slopes. Never infer missing one-second ticks."""
    base = {"status": "unavailable", "reversal": False, "version": VERSION}
    if (not isinstance(reference, dict) or reference.get("status") != "fresh"
            or reference.get("source") != INDEX_SOURCE):
        return {**base, "reason": "Fresh official BRTI reference required"}
    points = {}
    for pair in reference.get("points") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        t, price = finite(pair[0]), finite(pair[1])
        if (t is not None and price is not None and price > 0
                and abs(t - round(t)) < .001):
            points[int(round(t))] = price
    if not points:
        return {**base, "reason": "No verified ticks"}
    latest = max(points)
    age = finite(reference.get("age_seconds"))
    if age is None or age > 15 or age < 0:
        return {**base, "reason": "Reference stale"}
    def slope(seconds):
        candidates = [t for t in points if latest-seconds-8 <= t <= latest-seconds]
        if not candidates:
            return None
        prior = max(candidates)
        return (points[latest]-points[prior])*60/(latest-prior)
    short = slope(30)
    medium = slope(90)
    long = slope(150)
    if short is None or (long is None and medium is None):
        return {**base, "reason": "Insufficient observed independent slopes"}
    longer = long if long is not None else medium
    volatility = max(points[latest]*.00012, abs(longer-short)*.5)
    reversal = (short * longer < 0 and abs(short) >= volatility*.45)
    horizon = finite(seconds_left)
    # Time to expiration determines how aggressively a reversal can matter.
    urgency = "LATE" if horizon is not None and horizon <= 300 else ("HALFWAY" if horizon is not None and horizon <= 600 else "EARLY")
    return {**base, "status": "observed", "reversal": bool(reversal),
            "direction": ("UP" if short > 0 else "DOWN") if reversal else None,
            "short_usd_per_min": round(short, 3),
            "long_usd_per_min": round(longer, 3),
            "volatility_proxy_usd": round(volatility, 3),
            "urgency": urgency,
            "latest_tick_epoch": latest,
            "note": "Observed BRTI microtrend conflict, not a reversal probability"}


def guarded_decision(path, reference, official_target, seconds_left,
                     ai_forecast=None, reversal=None):
    """Create a versioned, independently testable research candidate.

    Stronger live BRTI paths dominate; early far-horizon momentum shrinks
    toward the contemporaneously observed index. The AI may make only a
    small, observed-range-supported adjustment, never overwrite the path.
    """
    base = {"status": "unavailable", "version": VERSION,
            "direction": "WAIT", "calibrated_probability": False,
            "live_order_placement": False}
    target = finite(official_target)
    left = finite(seconds_left)
    path = path if isinstance(path, dict) else {}
    reference = reference if isinstance(reference, dict) else {}
    if (path.get("status") != "available"
            or path.get("quality") != "OBSERVED"
            or reference.get("status") != "fresh"
            or reference.get("source") != INDEX_SOURCE
            or target is None or target <= 0
            or left is None or not 0 < left <= 900):
        return {**base, "reason": "Fresh observed BRTI path and verified target required"}
    forecast = finite(path.get("forecast_final_mean_usd"))
    current = finite(path.get("current_reference_usd"))
    path_target = finite(path.get("target_usd"))
    vol = finite(path.get("volatility_reference_usd_per_min"))
    if (forecast is None or current is None or vol is None
            or forecast <= 0 or current <= 0 or vol <= 0
            or path_target is None or abs(path_target-target) > .02):
        return {**base, "reason": "Invalid or target-mismatched path numbers"}

    # Hard-coded prospective policy, not a fit on the outcome being predicted.
    regime = "EARLY" if left > 600 else "HALFWAY" if left > 300 else "LATE"
    path_weight = {"EARLY": .35, "HALFWAY": .75, "LATE": 1.0}[regime]
    predicted = current + path_weight * (forecast-current)
    reverse = reversal if isinstance(reversal, dict) else {}
    adjustment = 0.0
    if reverse.get("status") == "observed" and reverse.get("reversal") is True:
        short = finite(reverse.get("short_usd_per_min"))
        longer = finite(reverse.get("long_usd_per_min"))
        if short is not None and longer is not None and short*longer < 0:
            # Conservative, horizon-limited reaction; no unlimited projection.
            raw = short * min(left/60, 2) * .12
            adjustment = clamp(raw, -vol*.25, vol*.25)
            predicted += adjustment

    ai_weight = 0.0
    ai = finite(ai_forecast)
    lo, hi = finite(path.get("downside_scenario_usd")), finite(path.get("upside_scenario_usd"))
    if (ai is not None and ai > 0 and lo is not None and hi is not None
            and lo <= ai <= hi and lo <= predicted <= hi):
        width = max(5.0, hi-lo)
        closeness = max(0.0, 1.0-abs(ai-predicted)/width)
        ai_weight = .05 * closeness
        predicted = predicted + ai_weight*(ai-predicted)

    if not math.isfinite(predicted) or predicted <= 0:
        return {**base, "reason": "Forecast failed numeric validation"}
    direction = "UP" if predicted >= target else "DOWN"
    # A forecast margin can rank evidence, but is NOT a calibrated odds model.
    margin = abs(predicted-target)
    uncertainty = max(5.0, vol * math.sqrt(max(.25, left/60)))
    quality = "STRONG" if margin >= 1.5*uncertainty else ("MIXED" if margin >= uncertainty else "LOW")
    return {**base, "status": "available", "direction": direction,
            "forecast_final_mean_usd": round(predicted, 2),
            "official_target_usd": round(target, 2),
            "margin_to_target_usd": round(margin, 2),
            "uncertainty_heuristic_usd": round(uncertainty, 2),
            "quality": quality, "regime": regime,
            "path_weight": path_weight, "ai_weight": round(ai_weight, 5),
            "reversal_adjustment_usd": round(adjustment, 2),
            "reversal_detected": bool(reverse.get("reversal")),
            "reason": "Path-first BRTI forecast, horizon shrinkage and observed reversal guard",
            "note": "Unvalidated prospective research candidate. Never interpret quality as win probability."}


def manual_entry_review(candidate, quote, market, direction=None, max_entry=.85):
    """Cost and breakeven review; do not imply uncalibrated profitable odds."""
    base = {"status": "unavailable", "action": "NO_TRADE_SIGNAL",
            "probability_calibrated": False, "orders_enabled": False}
    if not isinstance(candidate, dict) or candidate.get("status") != "available":
        return {**base, "reason": "Verified prospective forecast unavailable"}
    direction = direction or candidate.get("direction")
    quote = quote if isinstance(quote, dict) else {}
    if (direction not in ("UP", "DOWN") or quote.get("status") != "available"
            or quote.get("market") != market):
        return {**base, "reason": "No verified matching market quote"}
    price = finite(quote.get("up_entry" if direction == "UP" else "down_entry"))
    if price is None or not 0 < price < 1:
        return {**base, "reason": "No valid executable-side quote"}
    # Conservative illustrative US fee estimate; current fee schedule
    # is handled by the existing research.entry_cost routine.
    from research import entry_cost
    cost = entry_cost(price)
    if cost.get("status") != "estimated":
        return {**base, "reason": "Entry costs unavailable"}
    expensive = price > max_entry
    weak = candidate.get("quality") == "LOW"
    action = "AVOID_HIGH_PRICE" if expensive else ("INSUFFICIENT_MARGIN" if weak else "MANUAL_REVIEW_ONLY")
    return {**base, "status": "estimated", "action": action,
            "reason": "Contract too expensive" if expensive else ("Forecast distance is small versus volatility" if weak else "Price and margin are reviewable, but no calibrated probability exists"),
            "entry_price": price, "estimated_cost": cost,
            "required_win_rate_pct": cost.get("breakeven_win_rate_pct"),
            "max_entry_price": max_entry,
            "expected_value_established": False,
            "note": "Quotes are not fills. No trade advice or guarantee. Fees and slippage are estimated."}
