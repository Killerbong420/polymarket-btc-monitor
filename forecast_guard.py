"""Guarded research-only price projections and official-target reconciliation.

Do not use an experimental forecast to overwrite an independent AI call.
Record it prospectively; evaluate later on matched, officially settled rounds.
"""
import math

SHADOW_VERSION = "anchored-brti-price-shadow-v1"


def finite_positive(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def validate_official_target(official, reference):
    """Compare only complete *observed* opening windows; official target always wins."""
    value = finite_positive(official)
    ref = reference if isinstance(reference, dict) else {}
    opening = finite_positive(ref.get("opening_average"))
    samples = ref.get("opening_average_samples")
    out = {
        "status": "unavailable",
        "official_target_usd": value,
        "reconstructed_opening_usd": opening,
        "opening_average_samples": samples if isinstance(samples, int) else None,
        "basis": "60 observed one-second BRTI opening ticks vs official metadata",
        "official_target_authoritative": True,
    }
    if value is None or opening is None or samples != 60:
        return out
    gap = round(opening - value, 4)
    return {**out,
        "status": "match" if abs(opening - value) <= .02 else "mismatch",
        "difference_usd": gap,
        "note": "A discrepancy never substitutes reconstructed values for the published price-to-beat.",
    }


def anchored_shadow_forecast(ai_forecast, path, reference, official_target):
    """Fixed conservative shadow; never replaces the live AI direction.

    Shrink unvalidated AI excursions toward the independent BRTI price path,
    bounded by its own explicitly heuristic scenario width. No fitting to
    historical settlement results; prospective comparisons only.
    """
    base = {"status": "unavailable", "version": SHADOW_VERSION,
            "kind": "shadow_only_not_live_signal",
            "weight_path": .85, "weight_ai": .15}
    p = path if isinstance(path, dict) else {}
    ref = reference if isinstance(reference, dict) else {}
    target = finite_positive(official_target)
    ai = finite_positive(ai_forecast)
    projected = finite_positive(p.get("forecast_final_mean_usd"))
    current = finite_positive(ref.get("price"))
    path_target = finite_positive(p.get("target_usd"))
    if (p.get("status") != "available" or p.get("quality") != "OBSERVED"
            or ref.get("status") != "fresh"
            or ref.get("source") != "Polymarket US BTC reference / BRTI"):
        return {**base, "reason": "Fresh observed official-index price path required"}
    if any(x is None for x in (target, ai, projected, current, path_target)):
        return {**base, "reason": "Numeric AI, reference, path and target all required"}
    if abs(path_target-target) > .02:
        return {**base, "reason": "Path target conflicts with the current official target"}
    high = finite_positive(p.get("upside_scenario_usd"))
    low = finite_positive(p.get("downside_scenario_usd"))
    if high is None or low is None or not low <= projected <= high:
        return {**base, "reason": "Unavailable or inconsistent scenario bounds"}
    # These scenario bounds are heuristic, not confidence limits.
    bound = max(5., (high-low)*.75)
    clipped = min(projected+bound,max(projected-bound,ai))
    blended = round(.85*projected+.15*clipped, 2)
    return {**base, "status": "available",
            "forecast_final_mean_usd": blended,
            "projected_direction": "UP" if blended >= target else "DOWN",
            "ai_was_clipped": abs(clipped-ai) > .005,
            "anchor_distance_usd": round(blended-projected, 2),
            "note": "Unvalidated prospectively recorded shadow; never overrides live AI, settlements or trading.",
            }
