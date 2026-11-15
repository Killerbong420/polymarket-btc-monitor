"""Read-only BTC market-shock classification from completed exchange candles.

A shock is *observed abnormal activity*, not a direction forecast or a
probability of winning a Polymarket market. Polymarket BRTI contains no
trade-volume data, so this module only accepts exchange BTC/USD candle volume.
"""
import math
import time


def measure_market_shock(candles, source="exchange BTC/USD", now_epoch=None):
    """Compare 5 completed minutes against the immediately preceding 20.

    Candle format: [epoch, low, high, open, close, base_asset_volume].
    Volatility is mean absolute 1-minute log return, not annualized volatility.
    Using one venue at a time avoids mixing different volume denominators.
    """
    missing = {"status": "unavailable", "level": "unavailable",
               "volume_multiple": None, "movement_multiple": None,
               "source": source, "reason": "Insufficient fresh exchange candles"}
    try:
        bars = list(candles or [])[-26:]
        if len(bars) < 26:
            return missing
        stamps = [int(float(b[0])) for b in bars]
        closes = [float(b[4]) for b in bars]
        volumes = [float(b[5]) for b in bars]
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            return {**missing, "reason": "Invalid BTC closing price"}
        if any(not math.isfinite(v) or v < 0 for v in volumes):
            return {**missing, "reason": "Invalid BTC trading volume"}
        if any(b - a != 60 for a, b in zip(stamps, stamps[1:])):
            return {**missing, "reason": "Missing or noncontiguous completed minutes"}
        current = time.time() if now_epoch is None else float(now_epoch)
        age = current - (stamps[-1] + 60)
        if not math.isfinite(age) or age < -3 or age > 120:
            return {**missing, "reason": "Stale or incomplete exchange candles"}
        volume_reference = sum(volumes[-25:-5]) / 20
        volume_recent = sum(volumes[-5:]) / 5
        absolute_returns = [abs(math.log(closes[i] / closes[i - 1]))
                            for i in range(1, len(closes))]
        movement_reference = sum(absolute_returns[:20]) / 20
        movement_recent = sum(absolute_returns[20:]) / 5
        if volume_reference <= 0 or movement_reference <= 0:
            return {**missing, "reason": "No reliable historical baseline"}
        vr = volume_recent / volume_reference
        mr = movement_recent / movement_reference
        level = ("shock" if vr >= 3.0 or mr >= 2.5 or
                 (vr >= 2.3 and mr >= 1.8) else
                 "elevated" if vr >= 1.7 or mr >= 1.7 else "normal")
        net_change = (closes[-1] / closes[-6] - 1) * 100
        return {
            "status": "available", "level": level,
            "volume_multiple": round(vr, 2),
            "movement_multiple": round(mr, 2),
            "net_5m_change_pct": round(net_change, 4),
            "direction": "UP" if net_change > 0 else "DOWN" if net_change < 0 else "FLAT",
            "sample_age_seconds": round(max(0, age), 1),
            "source": source,
            "baseline": "Latest 5 completed 1m candles vs preceding 20 completed minutes",
            "note": "Observed spot exchange activity only; not official BRTI volume, a trade signal or calibrated risk probability.",
        }
    except (IndexError, KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
        return {**missing, "reason": "Invalid exchange candle data"}


BRTI_SOURCE = "Polymarket US BTC reference / BRTI"


def observe_flash_move(reference, now_epoch=None):
    """Read-only rapid-move alert from genuinely observed one-second BRTI ticks.

    Detect a $500 / 0.7% peak-to-trough move over the last five minutes,
    including before the five-minute window finishes. A $250 / 0.4%
    one-minute change is an early warning. This never decides settlement,
    changes the authoritative target, or places orders.
    """
    missing = {
        "status": "unavailable", "level": "unavailable", "direction": None,
        "source": BRTI_SOURCE, "reason": "Fresh dense official BRTI ticks required",
    }
    if not isinstance(reference, dict) or reference.get("status") != "fresh" or reference.get("source") != BRTI_SOURCE:
        return missing
    now = time.time() if now_epoch is None else float(now_epoch)
    if not math.isfinite(now):
        return missing
    observed = {}
    for tick in reference.get("points") or ():
        if not isinstance(tick, (list, tuple)) or len(tick) != 2:
            continue
        try:
            stamp, price = float(tick[0]), float(tick[1])
        except (TypeError, ValueError, OverflowError):
            continue
        if (math.isfinite(stamp) and math.isfinite(price) and price > 0
                and stamp.is_integer() and stamp <= now + 2):
            observed[int(stamp)] = price
    if not observed:
        return missing
    latest = max(observed)
    age = now - latest
    if not -2 <= age <= 15:
        return {**missing, "reason": "Official BRTI observation is stale or future-dated"}
    window = sorted((t, p) for t, p in observed.items() if latest - 300 <= t <= latest)
    if len(window) < 55 or window[-1][0] - window[0][0] < 59:
        return {**missing, "reason": "Not enough actual observation history"}
    span = window[-1][0] - window[0][0]
    if len(window) / (span + 1) < .90 or any(b[0] - a[0] > 10 for a, b in zip(window, window[1:])):
        return {**missing, "reason": "Observed BRTI tick gaps; no synthetic coverage"}

    peak = trough = window[0][1]
    peak_at = trough_at = window[0][0]
    max_fall = max_rise = 0.0
    fall_peak = rise_trough = window[0][1]
    fall_at = rise_at = window[0][0]
    for t, price in window[1:]:
        if price > peak:
            peak, peak_at = price, t
        if price < trough:
            trough, trough_at = price, t
        if peak - price > max_fall:
            max_fall, fall_peak, fall_at = peak - price, peak, t
        if price - trough > max_rise:
            max_rise, rise_trough, rise_at = price - trough, trough, t

    anchor = latest - 60
    anchors = [(abs(t - anchor), t, p) for t, p in window if abs(t - anchor) <= 4]
    change_60 = None
    if anchors:
        _, _, old_price = min(anchors)
        change_60 = window[-1][1] - old_price

    big_fall = max_fall >= 500 or max_fall / fall_peak >= .007
    big_rise = max_rise >= 500 or max_rise / rise_trough >= .007
    down_60 = change_60 is not None and (change_60 <= -250 or change_60 / window[-1][1] <= -.004)
    up_60 = change_60 is not None and (change_60 >= 250 or change_60 / window[-1][1] >= .004)
    if big_fall or big_rise:
        # If a crash is followed by a genuine rally, show the newer extreme.
        down = big_fall and (not big_rise or fall_at >= rise_at)
        level = "flash_selloff" if down else "flash_rally"
    elif down_60 or up_60:
        down = down_60 and (not up_60)
        level = "rapid_selloff" if down else "rapid_rally"
    else:
        down, level = False, "normal"
    return {
        "status": "available", "level": level,
        "direction": ("DOWN" if down else "UP") if level != "normal" else None,
        "source": BRTI_SOURCE, "latest_tick_epoch": latest,
        "sample_age_seconds": round(max(0, age), 1),
        "window_seconds": span, "observed_ticks": len(window),
        "coverage_pct": round(100 * len(window) / (span + 1), 1),
        "five_minute_drawdown_usd": round(max_fall, 2),
        "five_minute_rally_usd": round(max_rise, 2),
        "last_60s_change_usd": round(change_60, 2) if change_60 is not None else None,
        "latest_reference_usd": round(window[-1][1], 2),
        "note": "Observed BRTI movement only; a past selloff can rebound. Not trade pressure, a settlement prediction, or a calibrated win probability.",
    }
