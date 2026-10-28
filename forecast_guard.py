"""Guarded research-only price projections and official-target reconciliation.

Do not use an experimental forecast to overwrite an independent AI call.
Record it prospectively; evaluate later on matched, officially settled rounds.
"""
import math

SHADOW_VERSION = "anchored-brti-price-shadow-v2"


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
    # Public one-second candles are close samples, not necessarily the exact
    # underlying benchmark observations or rounding behind published terms.
    # Keep any discrepancy visible, but distinguish a small reconstruction
    # variation from an operationally material mismatch. Never replace target.
    material = abs(gap) > max(10.0, value * .00015)
    return {**out,
        "status": "match" if abs(gap) <= .02 else "mismatch",
        "difference_usd": gap,
        "difference_pct": round(100 * gap / value, 6),
        "material": material,
        "classification": "MATERIAL_DIVERGENCE" if material else "RECONSTRUCTION_VARIANCE",
        "note": "One-second candle closes may differ from benchmark sampling. Published price-to-beat always remains authoritative; investigate material divergence rather than silently substituting reconstructed values.",
    }


def anchored_shadow_forecast(ai_forecast, path, reference, official_target):
    """Fixed conservative shadow; never replaces the live AI direction.

    A guarded, prospectively scored research candidate. Give the stronger
    observed BRTI path at least 95% weight, and use at most 5% of the AI
    displacement *only* when it lies inside the path scenario width. Those
    scenario bounds are heuristic, not a confidence interval. Unvalidated
    historical performance is never claimed as an out-of-sample improvement.
    """
    base = {"status": "unavailable", "version": SHADOW_VERSION,
            "kind": "shadow_only_not_live_signal",
            "weight_path": 1.0, "weight_ai": 0.0}
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
    # Reject distant AI extrapolations rather than allowing a 15% adjustment
    # merely because broad volatility scenarios can contain them.
    width = max(5., high-projected, projected-low)
    gap = ai-projected
    relative_distance = abs(gap)/width
    ai_weight = .05 * max(0., 1.-relative_distance)
    # Preserve the independently projected BRTI settlement mean exactly when
    # the AI estimate is outside the scenario width, without changing the
    # raw AI decision, signal, or archived forecast.
    blended = round(projected + ai_weight*gap, 2)
    return {**base, "status": "available",
            "weight_path": round(1.-ai_weight, 6),
            "weight_ai": round(ai_weight, 6),
            "forecast_final_mean_usd": blended,
            "projected_direction": "UP" if blended >= target else "DOWN",
            "ai_was_clipped": relative_distance >= 1.,
            "ai_path_disagreement_usd": round(gap, 2),
            "anchor_distance_usd": round(blended-projected, 2),
            "note": "Unvalidated prospectively scored shadow only; independent live AI directions, official settlements and trading remain unchanged.",
            }
