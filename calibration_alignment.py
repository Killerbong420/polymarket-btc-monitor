"""Prevent time-to-expiry leakage in empirical 15m gap probabilities.

Our historic labelled source contains HALFWAY checkpoint snapshots only.
A halfway gap cannot legitimately calibrate a live gap from minute 1 or 14.
Use existing checkpoint band (450..390 seconds left) and a fresh, coherent
official BRTI state. No data is backfilled, no new polling or DB reads.
"""
import math
from datetime import datetime, timezone

HALFWAY_MIN_SECONDS_LEFT = 390
HALFWAY_MAX_SECONDS_LEFT = 450
OFFICIAL_SOURCE = "Polymarket US BTC reference / BRTI"


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _in_halfway_window(left):
    remaining = _finite(left)
    return remaining is not None and HALFWAY_MIN_SECONDS_LEFT <= remaining <= HALFWAY_MAX_SECONDS_LEFT


def match_halfway_live_state(behavior, reference, seconds_left, updated_at, now_utc):
    """Return eligibility reason for showing checkpoint-matched historical odds.

    The main live BRTI path forecast is unaffected; only EMPIRICAL historic odds
    from frozen 7:30-left checkpoints are restricted to comparable live times.
    """
    base = {"status": "unavailable", "cohort": "HALFWAY", "version": "halfway-time-match-v1"}
    remaining = _finite(seconds_left)
    if not _in_halfway_window(remaining):
        return {**base, "reason": "no_same_time_checkpoint_history"}
    if not isinstance(behavior, dict) or not isinstance(reference, dict):
        return {**base, "reason": "missing_verified_brti_reference"}
    if (behavior.get("status") != "available" or behavior.get("is_reference") is not True
            or behavior.get("source") != OFFICIAL_SOURCE
            or reference.get("status") != "fresh" or reference.get("source") != OFFICIAL_SOURCE):
        return {**base, "reason": "missing_verified_brti_reference"}
    age = _finite(behavior.get("price_age_seconds"))
    index_age = _finite(reference.get("age_seconds"))
    if (age is None or index_age is None or age < 0 or index_age < 0
            or age > 15 or index_age > 15):
        return {**base, "reason": "stale_brti_reference"}
    elapsed = _finite(behavior.get("elapsed_seconds"))
    if elapsed is None or abs(elapsed - (900 - remaining)) > 20:
        return {**base, "reason": "market_clock_mismatch"}
    try:
        timestamp = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return {**base, "reason": "missing_fresh_live_cycle"}
        updated_seconds = timestamp.timestamp()
    except (TypeError, ValueError, AttributeError):
        return {**base, "reason": "missing_fresh_live_cycle"}
    now = _finite(now_utc)
    if now is None or now - updated_seconds > 15 or updated_seconds - now > 2:
        return {**base, "reason": "stale_live_cycle"}
    return {"status": "matched", "cohort": "HALFWAY",
            "version": "halfway-time-match-v1",
            "seconds_left_band": [HALFWAY_MIN_SECONDS_LEFT, HALFWAY_MAX_SECONDS_LEFT],
            "note": "Historic odds apply only to same-minute halfway BRTI checkpoints."}


def eligible_halfway_history(snapshot):
    """Reject historical halfway labels recorded at the wrong time/source."""
    if not isinstance(snapshot, dict):
        return False
    if not _in_halfway_window(snapshot.get("seconds_left")):
        return False
    behavior = snapshot.get("target_behavior")
    reference = snapshot.get("reference_price")
    if not isinstance(behavior, dict) or not isinstance(reference, dict):
        return False
    if (behavior.get("status") != "available" or behavior.get("is_reference") is not True
            or behavior.get("source") != OFFICIAL_SOURCE
            or reference.get("status") != "fresh" or reference.get("source") != OFFICIAL_SOURCE):
        return False
    age = _finite(behavior.get("price_age_seconds"))
    index_age = _finite(reference.get("age_seconds"))
    elapsed = _finite(behavior.get("elapsed_seconds"))
    remaining = _finite(snapshot.get("seconds_left"))
    return (age is not None and index_age is not None and elapsed is not None
            and 0 <= age <= 15 and 0 <= index_age <= 15
            and abs(elapsed - (900 - remaining)) <= 20
            and _finite(behavior.get("target_gap_pct")) is not None)
