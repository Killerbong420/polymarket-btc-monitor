"""Forward-only 60-second BRTI momentum and reversal forecast.

Not Polymarket 15-minute settlement direction; no invented probabilities, odds,
exchange-price substitutes, or trades. The model uses only observed index ticks.
"""
import math
import statistics
import time

VERSION = "brti-quick-60s-v1"
INDEX_SOURCE = "Polymarket US BTC reference / BRTI"


def _positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def quick_forecast(reference, now=None):
    """Estimate BRTI movement over the NEXT minute, not the current target side."""
    base = {"status": "unavailable", "version": VERSION,
            "horizon_seconds": 60, "source": INDEX_SOURCE,
            "quantity": "future_BRTI_index_tick_in_60_seconds",
            "calibrated_probability": False}
    ref = reference if isinstance(reference, dict) else {}
    if ref.get("status") != "fresh" or ref.get("source") != INDEX_SOURCE:
        return {**base, "reason": "Fresh official BRTI reference is required"}
    now = time.time() if now is None else _positive(now)
    if now is None:
        return {**base, "reason": "Invalid current time"}
    # Never read future samples, synthetic gaps, or ticks from other assets.
    ticks = {}
    for pair in ref.get("points") or []:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        t, p = _positive(pair[0]), _positive(pair[1])
        if t is None or p is None:
            continue
        if abs(t-round(t)) > .001:
            continue
        if now-240 <= t <= now:
            ticks[int(round(t))] = p
    if len(ticks) < 20:
        return {**base, "reason": "Insufficient observed BRTI ticks"}
    latest = max(ticks)
    price = ticks[latest]
    age = now-latest
    if age < 0 or age > 12:
        return {**base, "reason": "BRTI observations stale", "age_seconds": round(age, 1)}
    if latest-min(ticks) < 45:
        return {**base, "reason": "Need at least 45 seconds of actual index history"}

    def anchor(seconds, tolerance=8):
        candidates = [t for t in ticks if latest-seconds-tolerance <= t <= latest-seconds]
        return max(candidates) if candidates else None

    slopes = {}
    for horizon in (12, 30, 60, 120):
        t0 = anchor(horizon)
        if t0 is not None and latest != t0:
            slopes[horizon] = (price-ticks[t0])*60/(latest-t0)
    if 12 not in slopes or 30 not in slopes:
        return {**base, "reason": "Short-horizon BRTI comparisons unavailable"}
    weights = {12: .30, 30: .35, 60: .25, 120: .10}
    norm = sum(weights[h] for h in slopes)
    pace = sum(slopes[h]*weights[h] for h in slopes)/norm

    # Realized microstructure noise, not a calibrated uncertainty interval.
    # Sample non-overlapping observed 10-second changes, rejecting gap crossings.
    moves = []
    for t in range(latest-80, latest-9, 10):
        a = next((x for x in (t, t-1, t-2) if x in ticks), None)
        b = next((x for x in (t+10, t+9, t+8) if x in ticks), None)
        if a is not None and b is not None and 7 <= b-a <= 12:
            moves.append(ticks[b]-ticks[a])
    noise = max(price*.000012, statistics.median([abs(v) for v in moves]) if moves else 0)
    short = slopes[12]
    medium = slopes[60] if 60 in slopes else slopes[30]
    opposite = short*medium < 0
    reversal = opposite and abs(short) >= max(2*noise, price*.000035)

    # Extrapolate only part of the observed movement: bursts typically decay.
    # A true opposing short trend can turn the projection but is labeled risky.
    predicted_delta = .55*pace
    max_move = max(5.0, price*.0015)
    predicted_delta = max(-max_move, min(max_move, predicted_delta))
    if abs(predicted_delta) < .005:
        return {**base, "status": "available", "direction": "WAIT",
                "quality": "NO_EDGE", "current_reference_usd": round(price, 2),
                "forecast_price_60s_usd": round(price, 2),
                "forecast_move_usd": 0.0, "age_seconds": round(age, 1),
                "reversal_watch": bool(reversal),
                "reason": "Observed 12s/30s trends cancel; no defendable 60s direction"}
    direction = "UP" if predicted_delta > 0 else "DOWN"
    agreement = sum(1 for slope in slopes.values() if slope*predicted_delta > 0)
    quality = ("HIGH" if agreement >= 3 and not opposite and abs(predicted_delta) >= 2*noise
               else "MEDIUM" if agreement >= 2 and abs(predicted_delta) >= noise
               else "LOW")
    # HIGH/MEDIUM/LOW describe indicator agreement, not empirical hit rates.
    return {**base, "status": "available", "direction": direction,
            "quality": quality,
            "current_reference_usd": round(price, 2),
            "forecast_price_60s_usd": round(max(.01, price+predicted_delta), 2),
            "forecast_move_usd": round(predicted_delta, 2),
            "age_seconds": round(age, 1), "input_spans_seconds": sorted(slopes),
            "short_pace_usd_per_min": round(short, 2),
            "longer_pace_usd_per_min": round(medium, 2),
            "reversal_watch": bool(reversal),
            "reversal_direction": ("UP" if short > 0 else "DOWN") if reversal else None,
            "reason": ("Possible short-term reversal; opposing 12s/longer index slopes"
                       if reversal else "Weighted 12s/30s/60s/120s BRTI movement with burst decay"),
            "note": "Next-minute BRTI movement only, not final 15m settlement or a probability."}
