"""Selective BTC price-action patterns for forward-only research.

Publicly described concepts such as market structure, range highs/lows,
failed breakouts, and liquidity sweeps inform these *independent* rules.
Not affiliated with or a reproduction of anyone's proprietary strategy.

Input: chronological completed 1-minute exchange OHLCV bars
       [unix_seconds, low, high, open, close, base_volume].
Output: a descriptive price-action context; WAIT is an intentional abstention.
This forecast is NOT a BRTI settlement probability or a trading instruction.
"""
import math

VERSION = "range-structure-v1"


def analyze_price_action(candles, source="exchange 1m"):
    def inactive(reason, **fields):
        return dict(version=VERSION, status="unavailable", direction="WAIT",
                    setup="NO_VALID_SETUP", bias="NEUTRAL", confidence="UNRATED",
                    reason=reason, source=source, **fields)

    try:
        if len(candles) < 35:
            return inactive("Need at least 35 completed 1-minute candles")
        bars = []
        for raw in candles[-40:]:
            ts, low, high, op, close, volume = [float(raw[i]) for i in range(6)]
            if not all(math.isfinite(x) for x in (ts, low, high, op, close, volume)):
                return inactive("Invalid or non-finite exchange candles")
            if min(low, high, op, close) <= 0 or low > min(op, close) or high < max(op, close) or volume < 0:
                return inactive("Invalid OHLC or volume values")
            bars.append((int(ts), low, high, op, close, volume))
    except (TypeError, IndexError, KeyError, ValueError, OverflowError):
        return inactive("Unreadable 1-minute candles")
    if any(b[0] - prev[0] != 60 for prev, b in zip(bars, bars[1:])):
        return inactive("Missing or noncontiguous 1-minute bars")

    history = bars[-32:-3]  # Fixed levels BEFORE the last three completed candles.
    a, previous, last = bars[-3:]
    hi = max(x[2] for x in history)
    lo = min(x[1] for x in history)
    close = last[4]
    ranges = [max(b[2] - b[1], abs(b[2] - a[4]), abs(b[1] - a[4]))
              for a, b in zip(bars[-15:-1], bars[-14:])]
    atr = sum(ranges) / len(ranges)
    buffer = max(close * 0.000035, atr * 0.12)
    range_pct = 100 * (hi - lo) / close
    if hi <= lo or atr <= 0 or not math.isfinite(range_pct):
        return inactive("Price range or volatility unavailable")

    def mean(xs):
        return sum(xs) / len(xs)
    c = [x[4] for x in bars]
    vol_base = mean([x[5] for x in history[-20:]])
    vol_recent = mean([x[5] for x in bars[-2:]])
    volume_ratio = round(vol_recent / vol_base, 2) if vol_base > 0 else None
    typical = mean([abs(c[i] - c[i - 1]) for i in range(-11, 0)])
    displacement = abs(c[-1] - c[-11])
    efficiency = displacement / (10 * typical) if typical > 0 else 0.0
    earlier, newer = bars[-23:-13], bars[-13:-3]
    higher = max(x[2] for x in newer) > max(x[2] for x in earlier) and min(x[1] for x in newer) > min(x[1] for x in earlier)
    lower = max(x[2] for x in newer) < max(x[2] for x in earlier) and min(x[1] for x in newer) < min(x[1] for x in earlier)
    structure = "HIGHER_HIGHS_AND_LOWS" if higher else "LOWER_HIGHS_AND_LOWS" if lower else "RANGE_OR_MIXED"
    bias = "UP" if higher else "DOWN" if lower else "NEUTRAL"

    # Fixed range levels prevent a current wick from moving the very level it tests.
    swept_high = last[2] > hi + buffer and last[4] < hi - buffer / 2
    swept_low = last[1] < lo - buffer and last[4] > lo + buffer / 2
    prior_swept_high = previous[2] > hi + buffer and previous[4] < hi and last[4] < hi - buffer / 2
    prior_swept_low = previous[1] < lo - buffer and previous[4] > lo and last[4] > lo + buffer / 2

    up_wick = last[2] - max(last[3], last[4])
    down_wick = min(last[3], last[4]) - last[1]
    body = max(abs(last[4] - last[3]), buffer / 2)
    support_reclaim = (swept_low and down_wick >= body * 0.9) or (
        prior_swept_low and last[4] >= previous[4] + buffer * 0.25)
    resistance_reject = (swept_high and up_wick >= body * 0.9) or (
        prior_swept_high and last[4] <= previous[4] - buffer * 0.25)

    # Require two *completed* closes beyond a prior range; a single wick is not a breakout.
    break_up = previous[4] > hi + buffer and last[4] > hi + buffer
    break_down = previous[4] < lo - buffer and last[4] < lo - buffer
    retest_up = break_up and last[1] <= hi + buffer and last[4] > hi + buffer
    retest_down = break_down and last[2] >= lo - buffer and last[4] < lo - buffer
    coherent_up = c[-1] > c[-2] >= c[-3]
    coherent_down = c[-1] < c[-2] <= c[-3]
    # Volume only confirms when a real exchange volume series exists.
    volume_ok = volume_ratio is not None and volume_ratio >= 0.85
    direction, setup, why = "WAIT", "RANGE_OR_CHOP", "No confirmed range break, retest or liquidity sweep"
    if support_reclaim and resistance_reject:
        setup, why = "TWO_SIDED_SWEEP", "Both range edges swept; direction ambiguous"
    elif support_reclaim:
        direction, setup, why = "UP", "LOW_SWEEP_RECLAIM", "Range-low sweep reclaimed with rejection confirmation"
    elif resistance_reject:
        direction, setup, why = "DOWN", "HIGH_SWEEP_REJECTION", "Range-high sweep rejected with candle confirmation"
    elif retest_up and volume_ok and coherent_up:
        direction, setup, why = "UP", "UP_BREAKOUT_RETEST", "Two closes above prior resistance; retest held with exchange volume"
    elif retest_down and volume_ok and coherent_down:
        direction, setup, why = "DOWN", "DOWN_BREAKOUT_RETEST", "Two closes below prior support; retest held with exchange volume"
    elif break_up and volume_ok and coherent_up and efficiency >= 0.32:
        direction, setup, why = "UP", "UP_BREAKOUT", "Two completed closes above range high and supported volume"
    elif break_down and volume_ok and coherent_down and efficiency >= 0.32:
        direction, setup, why = "DOWN", "DOWN_BREAKOUT", "Two completed closes below range low and supported volume"
    elif swept_high or swept_low or prior_swept_high or prior_swept_low:
        setup, why = "UNCONFIRMED_LIQUIDITY_SWEEP", "Range swept without required rejection confirmation"
    elif break_up or break_down:
        setup, why = "UNCONFIRMED_BREAKOUT", "Breakout lacks volume or continuation confirmation"
    elif efficiency < 0.32:
        setup, why = "CHOP", "Overlapping short-term candles; wait for range resolution"
    elif higher or lower:
        setup, why = "STRUCTURE_WITHIN_RANGE", "Trend structure alone does not confirm a breakout"
    return {
        "version": VERSION, "status": "ready", "direction": direction, "bias": bias,
        "setup": setup, "confidence": "UNRATED", "reason": why, "source": source,
        "timeframe": "1m completed bars; recent 40m context", "price": round(close, 2),
        "support": round(lo, 2), "resistance": round(hi, 2),
        "range_pct": round(range_pct, 4), "atr_usd": round(atr, 2),
        "threshold_usd": round(buffer, 2), "structure": structure,
        "efficiency": round(efficiency, 3), "volume_ratio": volume_ratio,
        "volume_confirmed": volume_ok, "swept_high": swept_high or prior_swept_high,
        "swept_low": swept_low or prior_swept_low,
        "note": "Public price-action concepts; independent deterministic interpretation, not trader alerts, probabilities or official BRTI outcomes.",
    }


def shadow_price_action(context, candle_age_seconds):
    """Snapshot-only prediction recorded alongside the genuine AI checkpoint."""
    if not isinstance(context, dict) or context.get("status") != "ready":
        return {"direction": "WAIT", "source": "PRICE_ACTION_ABSTAIN", "confidence": "UNRATED",
                "reason": "No validated exchange price-action context"}
    try:
        age = float(candle_age_seconds)
    except (TypeError, ValueError):
        age = float("inf")
    if not math.isfinite(age) or age < 0 or age > 75:
        return {"direction": "WAIT", "source": "PRICE_ACTION_ABSTAIN", "confidence": "UNRATED",
                "reason": "Completed exchange candles stale"}
    direction = context.get("direction")
    eligible = direction in ("UP", "DOWN")
    return {"direction": direction if eligible else "WAIT",
            "source": "PRICE_ACTION_V1" if eligible else "PRICE_ACTION_ABSTAIN",
            "confidence": "UNRATED", "setup": context.get("setup"),
            "reason": context.get("reason", "Pattern missing")}
