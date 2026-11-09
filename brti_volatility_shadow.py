"""Read-only zero-drift BRTI volatility baseline for US BTC 15-minute contracts.

Independent research inspired by public binary-market fair-value modeling.
Unlike a spot-at-expiry binary, the US contract settles on the final 60-second
BRTI average. For a Brownian price approximation with T>=60 seconds left,
variance of that final-minute average is sigma_per_second**2 * (T - 40).
This is a theoretical assumption, NOT a calibrated probability, a proven edge,
a production forecast, or permission to trade. No external APIs or AI.
"""
import json
import math

VERSION = "brti-zero-drift-60s-mean-shadow-v1"
SOURCE = "Polymarket US BTC reference / BRTI"


def _positive(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def brti_volatility_shadow(reference, path, official_target, seconds_left,
                           target_validation=None):
    """One prospectively frozen research-only probability, with fail-closed guards."""
    result = {"status": "unavailable", "direction": "WAIT", "source": VERSION,
              "confidence": "UNRATED", "research_only": True,
              "probabilities_calibrated": False, "orders_enabled": False}
    ref = reference if isinstance(reference, dict) else {}
    estimate = path if isinstance(path, dict) else {}
    if isinstance(target_validation, dict) and target_validation.get("material") is True:
        return {**result, "reason": "Material official BRTI target discrepancy"}
    if ref.get("source") != SOURCE or ref.get("status") != "fresh":
        return {**result, "reason": "Official fresh BRTI observations required"}
    if estimate.get("status") != "available" or estimate.get("quality") != "OBSERVED":
        return {**result, "reason": "Observed BRTI volatility estimate required"}

    current = _positive(ref.get("price"))
    target = _positive(official_target)
    path_target = _positive(estimate.get("target_usd"))
    volatility = _positive(estimate.get("volatility_reference_usd_per_min"))
    remaining = _positive(seconds_left)
    age = _positive(ref.get("age_seconds")) if ref.get("age_seconds") != 0 else 0.0
    if (None in (current, target, path_target, volatility, remaining, age)
            or remaining > 900 or age > 15
            or volatility > current * 0.1):
        return {**result, "reason": "Missing, stale or implausible BRTI inputs"}
    if abs(path_target - target) > .02:
        return {**result, "reason": "Path target does not match published official price-to-beat"}
    if remaining <= 60:
        return {**result, "reason": "Final-minute averaging underway; model intentionally abstains"}

    # If B(t) is Brownian motion in seconds, the mean of B(t) over
    # [T-60, T] has variance T-60 + 60/3 = T-40.
    effective_seconds = remaining - 40.0
    sigma_usd = volatility * math.sqrt(effective_seconds / 60.0)
    if not math.isfinite(sigma_usd) or sigma_usd <= 0:
        return {**result, "reason": "Unusable volatility scale"}
    z = (current - target) / sigma_usd
    up = max(.01, min(.99, .5 * math.erfc(-z / math.sqrt(2.0))))
    direction = "UP" if up > .5 else "DOWN" if up < .5 else "WAIT"
    return {**result, "status": "available", "direction": direction,
            "model_up_probability": round(up, 6),
            "model_down_probability": round(1.0 - up, 6),
            "reference_price_usd": round(current, 2),
            "official_target_usd": round(target, 2),
            "input_volatility_usd_per_min": round(volatility, 4),
            "effective_variance_seconds": round(effective_seconds, 2),
            "seconds_left": round(remaining, 2),
            "reason": "Zero-drift Brownian benchmark adjusted for final 60-second mean",
            "note": "Theoretical, NOT empirically calibrated; separate from main direction meter and all live decisions."}


def evaluate_shadow(rows):
    """Read-only scores after official binary settlement; no model promotion.

    Expected rows: (market, checkpoint, calls_json, official_winner). Caller
    must query only official market settlement rows in chronological order.
    """
    groups = {}
    seen = set()
    for market, checkpoint, raw, winner in rows:
        if not market or winner not in ("UP", "DOWN"):
            continue
        key = (market, checkpoint)
        if key in seen:
            continue
        try:
            calls = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(calls, dict):
            continue
        signal = calls.get("BRTI_VOL_SHADOW")
        if (not isinstance(signal, dict) or signal.get("source") != VERSION
                or signal.get("status") != "available"):
            continue
        p = _positive(signal.get("model_up_probability"))
        if p is None or p >= 1:
            continue
        seen.add(key)
        y = int(winner == "UP")
        group = groups.setdefault(checkpoint, {
            "checkpoint": checkpoint, "officially_settled_rounds": 0,
            "sum_brier": 0.0, "correct": 0, "up_wins": 0
        })
        group["officially_settled_rounds"] += 1
        group["sum_brier"] += (p-y)**2
        group["up_wins"] += y
        group["correct"] += int((p > .5 and y == 1) or (p < .5 and y == 0))
    summaries = []
    for checkpoint, g in sorted(groups.items()):
        n = g["officially_settled_rounds"]
        summaries.append({
            "checkpoint": checkpoint, "version": VERSION,
            "officially_settled_rounds": n,
            "model_brier": round(g["sum_brier"]/n, 5),
            "neutral_50_50_brier": .25,
            "directional_accuracy_pct": round(100*g["correct"]/n, 1),
            "observed_up_frequency_pct": round(100*g["up_wins"]/n, 1),
            "sample_sufficient_for_review": n >= 100
        })
    return {"status": "observed_research_only" if summaries else "collecting",
            "version": VERSION, "auto_promote": False,
            "cohorts": summaries,
            "limitations": "Only official later-settled matched checkpoint calls. The neutral 0.5 Brier benchmark is descriptive, not chronological holdout evidence; scores must be tested on unseen subsequent rounds before any use in live percentages."}
