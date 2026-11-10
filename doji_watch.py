"""Read-only early Doji formation watch from observed Polymarket US BRTI ticks.

A forming Doji is a candle-shape *watch*, not an UP/DOWN settlement prediction,
not a calibrated probability and not a completed candlestick. No extra polling.
"""
import math

SOURCE = "Polymarket US BTC reference / BRTI"
VERSION = "brti-preclose-doji-v1"


def _candle(points, start, end, minimum_last, minimum_count):
    observed = [(t, p) for t, p in points if start <= t < end]
    if (len(observed) < minimum_count or observed[0][0] > start + 3
            or observed[-1][0] < minimum_last
            or any(b[0] - a[0] > 6 for a, b in zip(observed, observed[1:]))):
        return None
    opening, close = observed[0][1], observed[-1][1]
    high = max(p for _, p in observed)
    low = min(p for _, p in observed)
    spread = high - low
    if spread < max(0.05, opening * 0.000003):
        return None
    ratio = abs(close - opening) / spread
    upper = (high - max(opening, close)) / spread
    lower = (min(opening, close) - low) / spread
    if lower >= 0.62 and upper <= 0.16:
        kind = "DRAGONFLY_DOJI"
    elif upper >= 0.62 and lower <= 0.16:
        kind = "GRAVESTONE_DOJI"
    elif upper >= 0.30 and lower >= 0.30:
        kind = "LONG_LEGGED_DOJI"
    else:
        kind = "STANDARD_DOJI"
    return {"pattern": kind, "body_to_range": ratio,
            "opening": round(opening, 2), "current": round(close, 2),
            "high": round(high, 2), "low": round(low, 2),
            "last_tick_epoch": observed[-1][0], "observed_ticks": len(observed),
            "observed": observed}


def anticipate_doji(reference, now=None):
    """Flag a near-close Doji candidate before a 1m BRTI candle is finished.

    A watch is allowed only 4-22 seconds BEFORE minute close, using genuine
    non-stale one-second BRTI observations. Completed-candle confirmation is
    reported separately, briefly after the boundary. This never changes calls.
    """
    base = {"status": "unavailable", "version": VERSION,
            "source": SOURCE, "influences_prediction": False,
            "probability_calibrated": False}
    if not isinstance(reference, dict) or reference.get("status") != "fresh" or reference.get("source") != SOURCE:
        return {**base, "reason": "fresh_official_brti_required"}
    try:
        clock = float(now) if now is not None else __import__("time").time()
        if not math.isfinite(clock) or clock <= 0:
            raise ValueError("bad_clock")
        unique = {}
        for item in reference.get("points") or []:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            t, p = float(item[0]), float(item[1])
            if (math.isfinite(t) and t == int(t) and math.isfinite(p)
                    and p > 0 and 0 <= clock - t <= 130):
                unique[int(t)] = p
        points = sorted(unique.items())
        if not points or not 0 <= clock - points[-1][0] <= 12:
            return {**base, "reason": "stale_or_missing_brti_ticks"}
        minute = int(clock // 60) * 60
        left = minute + 60 - clock
        if 4 <= left <= 22:
            candle = _candle(points, minute, minute + 60,
                             minute + 37, 25)
            if candle is None:
                return {**base, "status": "monitoring",
                        "reason": "incomplete_current_minute_coverage"}
            ratio = candle["body_to_range"]
            earlier = [(t, p) for t, p in candle["observed"]
                       if candle["last_tick_epoch"] - 15 <= t <= candle["last_tick_epoch"] - 7]
            approaching_open = bool(earlier and
                                    abs(candle["current"] - candle["opening"]) <
                                    abs(earlier[-1][1] - candle["opening"]))
            if ratio <= 0.12 or (ratio <= 0.20 and approaching_open):
                candle.pop("observed")
                return {**base, **candle, "status": "forming",
                        "seconds_until_close": round(left, 1),
                        "candle_end_epoch": minute + 60,
                        "body_to_range_pct": round(ratio * 100, 1),
                        "reason": "Near-open body before the candle finishes; can invalidate at close"}
            return {**base, "status": "monitoring", "reason": "no_doji_shape_yet",
                    "seconds_until_close": round(left, 1)}
        # Confirmation is retrospective, never portrayed as an early alert.
        if clock - minute <= 12:
            candle = _candle(points, minute - 60, minute,
                             minute - 4, 40)
            if candle and candle["body_to_range"] <= 0.12:
                candle.pop("observed")
                return {**base, **candle, "status": "confirmed",
                        "candle_end_epoch": minute,
                        "body_to_range_pct": round(candle["body_to_range"] * 100, 1),
                        "reason": "Previous completed minute's Doji shape confirmed"}
        return {**base, "status": "monitoring",
                "reason": "Watching the final 22 seconds of each one-minute candle"}
    except (TypeError, ValueError, OverflowError, IndexError):
        return {**base, "reason": "invalid_reference_samples"}
