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
