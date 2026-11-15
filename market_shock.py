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
        net_usd = closes[-1] - closes[-6]
        # A large absolute/relative five-minute move is noteworthy even
        # when the preceding 20 minutes were already unusually volatile.
        rapid = abs(net_usd) >= 500 or abs(net_change) >= .45
        if rapid:
            level = "shock"
        return {
            "status": "available", "level": level,
            "volume_multiple": round(vr, 2),
            "movement_multiple": round(mr, 2),
            "net_5m_change_pct": round(net_change, 4),
            "net_5m_change_usd": round(net_usd, 2),
            "rapid_move": bool(rapid),
            "direction": "UP" if net_change > 0 else "DOWN" if net_change < 0 else "FLAT",
            "sample_age_seconds": round(max(0, age), 1),
            "source": source,
            "baseline": "Latest 5 completed 1m candles vs preceding 20 completed minutes",
            "note": "Observed spot exchange activity only; not official BRTI volume, a trade signal or calibrated risk probability.",
        }
    except (IndexError, KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
        return {**missing, "reason": "Invalid exchange candle data"}


def _observed_one_minute_impulse(ticks, latest, age, base):
    """Early warning only: at least 45 authentic ticks around the prior minute."""
    anchors = [t for t in ticks if abs(t-(latest-60)) <= 8]
    if not anchors:
        return None
    anchor = min(anchors, key=lambda t: abs(t-(latest-60)))
    times = sorted(t for t in ticks if anchor <= t <= latest)
    if (len(times) < 45 or len(times)/(latest-anchor+1) < .72 or
            any(b-a > 12 for a,b in zip(times,times[1:]))):
        return None
    change = ticks[latest] - ticks[anchor]
    pct = 100*change/ticks[anchor]
    if abs(change) < 250 and abs(pct) < .4:
        return None
    return {**base, "status": "available",
            "kind": "rapid_selloff" if change < 0 else "rapid_rally",
            "change_usd": round(change,2), "change_pct": round(pct,4),
            "change_60s_usd": round(change,2),
            "phase": "early_selloff" if change < 0 else "early_rally",
            "age_seconds": round(age,1), "lookback_seconds": latest-anchor,
            "latest_tick_epoch": latest,
            "basis": "Observed one-minute official index movement",
            "note": "Early observed price-move warning; not 15-minute settlement prediction, executed sell volume, or trading instruction."}


def measure_brti_rapid_move(reference, now_epoch=None):
    """Observe a five-minute BRTI move without waiting for minute candle closes.

    Official index observations are not Coinbase trades and do not imply the
    next 15-minute settlement. Require near-contiguous actual one-second ticks;
    never fill a missing five-minute anchor or accept stale/future data.
    """
    base = {"status": "unavailable", "kind": "unavailable",
            "source": "Polymarket US BTC reference / BRTI",
            "reason": "Fresh five-minute observed BRTI history required",
            "predictive": False}
    ref = reference if isinstance(reference, dict) else {}
    if ref.get("status") != "fresh" or ref.get("source") != base["source"]:
        return base
    try:
        now = time.time() if now_epoch is None else float(now_epoch)
        if not math.isfinite(now):
            return base
        ticks = {}
        for pair in ref.get("points") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            try:
                t, p = float(pair[0]), float(pair[1])
            except (TypeError, ValueError, OverflowError):
                continue
            if (math.isfinite(t) and math.isfinite(p) and p > 0
                    and abs(t-round(t)) < .001 and now-325 <= t <= now):
                ticks[int(round(t))] = p
        if not ticks:
            return base
        latest = max(ticks)
        age = now-latest
        if age < 0 or age > 12:
            return {**base, "reason": "BRTI ticks stale"}
        early = _observed_one_minute_impulse(ticks, latest, age, base)
        start = latest-300
        anchors = [t for t in ticks if abs(t-start) <= 8]
        if not anchors:
            return early if early is not None else {**base, "reason": "No observed five-minute anchor"}
        anchor = min(anchors, key=lambda t: abs(t-start))
        window = sorted(t for t in ticks if anchor <= t <= latest)
        if len(window) < 200 or any(b-a > 12 for a,b in zip(window,window[1:])):
            return early if early is not None else {**base, "reason": "Incomplete five-minute BRTI coverage"}
        change = ticks[latest]-ticks[anchor]
        pct = 100*change/ticks[anchor]
        rapid = abs(change) >= 500 or abs(pct) >= .45
        kind = ("rapid_selloff" if change < 0 else "rapid_rally") if rapid else "normal"
        # A fresh one-minute plunge deserves an early warning even while the
        # net five-minute change remains below the larger move threshold.
        if not rapid and early is not None:
            return early
        short_anchors = [t for t in ticks if abs(t-(latest-60)) <= 8]
        short_change = (ticks[latest]-ticks[min(short_anchors,key=lambda t:abs(t-(latest-60)))]) if short_anchors else None
        phase = "unknown"
        if short_change is not None:
            if change < 0:
                phase = "rebounding" if short_change > 0 else "continuing_down" if short_change < 0 else "stabilizing"
            elif change > 0:
                phase = "pulling_back" if short_change < 0 else "continuing_up" if short_change > 0 else "stabilizing"
            else:
                phase = "flat"
        return {**base, "status": "available", "kind": kind,
                "change_usd": round(change, 2), "change_pct": round(pct, 4),
                "change_60s_usd": round(short_change, 2) if short_change is not None else None,
                "phase": phase, "age_seconds": round(age, 1),
                "lookback_seconds": latest-anchor, "latest_tick_epoch": latest,
                "basis": "Observed five-minute official index movement",
                "note": "Observed official BRTI movement only; no inferred future direction, trade, or calibrated probability."}
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        return {**base, "reason": "Invalid BRTI observations"}
