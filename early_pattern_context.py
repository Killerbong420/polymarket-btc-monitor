"""Independent, causal BTC chart-formation context on *completed* 1m exchange bars.

Flags and double tops/bottoms are research WATCHES. A flag consolidation can be
seen *before* its possible breakout. Levels and status are measured against
already-observed candles; no future bars, settlement probabilities or order calls.
No Sonar feed or Sonar proprietary logic is involved.
"""
import math

VERSION = "independent-pattern-map-v1"


def _mean(values):
    return sum(values) / len(values)


def _flag_candidate(bars, offset, atr):
    """Five-candle directional pole followed by four smaller counter-move candles.

    offset=0: developing flag includes latest completed candle.
    offset=1: the latest bar can test an *already formed* flag boundary.
    offset=2: two latest bars can show a breakout then a retest.
    """
    end = len(bars) - 5 - offset
    start = end - 5
    pole = bars[end][4] - bars[start][4]
    size = abs(pole)
    if size < max(2.0 * atr, bars[-1][4] * .00045):
        return None
    direction = 1 if pole > 0 else -1
    tail = bars[end + 1:end + 5]
    if len(tail) != 4:
        return None
    end_price = bars[end][4]
    tail_high = max(b[2] for b in tail)
    tail_low = min(b[1] for b in tail)
    retreat = direction * (end_price - tail[-1][4]) / size
    # A flag consolidates against the impulse, but cannot retrace most of it.
    if (not .08 <= retreat <= .67 or tail_high-tail_low > .85*size or
            direction * (tail[-1][4] - bars[start][4]) < .30*size):
        return None
    # Strong moves during the consolidation disqualify the simple flag model.
    if direction > 0 and (tail_high > end_price + .27*size or tail_low < end_price - .78*size):
        return None
    if direction < 0 and (tail_low < end_price - .27*size or tail_high > end_price + .78*size):
        return None
    buffer = max(.12*atr, bars[-1][4]*.000012)
    trigger = tail_high + buffer if direction > 0 else tail_low - buffer
    invalidation = end_price - direction*.75*size
    return {"pattern": "BULL_FLAG" if direction > 0 else "BEAR_FLAG",
            "bias": "UP" if direction > 0 else "DOWN",
            "direction_sign": direction, "trigger": trigger,
            "invalidation": invalidation, "pole_usd": round(size, 2),
            "retracement_pct": round(100*retreat, 1)}


def _double_turn(bars, atr):
    """Two separated observed extrema close in price; confirmation is not assumed."""
    recent = bars[-19:]
    buffer = max(.22*atr, recent[-1][4]*.00002)
    candidates = []
    for sign, name, bias in ((1, "DOUBLE_TOP", "DOWN"), (-1, "DOUBLE_BOTTOM", "UP")):
        values = [b[2] if sign > 0 else b[1] for b in recent]
        pivots = [i for i in range(1, len(recent)-1) if
                  (values[i] > values[i-1] and values[i] >= values[i+1]) if sign > 0]
        if sign < 0:
            pivots = [i for i in range(1, len(recent)-1) if
                      values[i] < values[i-1] and values[i] <= values[i+1]]
        for newest in reversed(pivots):
            if len(recent) - 1 - newest > 5:
                continue
            for older in reversed(pivots):
                if older >= newest-3 or newest-older > 13:
                    continue
                if abs(values[newest]-values[older]) > .55*atr:
                    continue
                middle = recent[older+1:newest]
                if not middle:
                    continue
                neckline = min(b[1] for b in middle) if sign > 0 else max(b[2] for b in middle)
                depth = (min(values[older], values[newest]) - neckline) if sign > 0 else (neckline-max(values[older], values[newest]))
                if depth < 1.1*atr:
                    continue
                close = recent[-1][4]
                invalidation = max(values[older],values[newest])+buffer if sign > 0 else min(values[older],values[newest])-buffer
                if (sign > 0 and close > invalidation) or (sign < 0 and close < invalidation):
                    phase = "INVALIDATED"
                elif (sign > 0 and close < neckline - buffer) or (sign < 0 and close > neckline + buffer):
                    phase = "NECKLINE_BREAK_WATCH"
                else:
                    phase = "FORMING"
                candidates.append({"status": "watch", "pattern":name, "phase":phase,
                                   "bias":bias, "trigger_usd":round(neckline,2),
                                   "invalidation_usd":round(invalidation,2),
                                   "observed_gap_candles":newest-older})
                break
            if candidates:
                break
    return candidates[0] if candidates else None


def analyze_early_patterns(candles, source="exchange BTC/USD"):
    base = {"status": "unavailable", "version": VERSION, "source":source,
            "probability_calibrated":False, "changes_primary_prediction":False,
            "timeframe":"completed_1m_exchange_candles"}
    try:
        if len(candles) < 35:
            return {**base,"reason":"need_35_completed_candles"}
        bars = []
        for raw in candles[-50:]:
            t, low, high, op, close, vol = (float(raw[i]) for i in range(6))
            if (not all(math.isfinite(x) for x in (t,low,high,op,close,vol))
                    or t != int(t) or low <= 0 or vol < 0 or
                    low > min(op,close) or high < max(op,close)):
                return {**base,"reason":"invalid_ohlcv"}
            bars.append((int(t),low,high,op,close,vol))
        if any(b[0]-a[0]!=60 for a,b in zip(bars,bars[1:])):
            return {**base,"reason":"missing_completed_candles"}
        ranges = [max(b[2]-b[1],abs(b[2]-a[4]),abs(b[1]-a[4]))
                  for a,b in zip(bars[-17:-1],bars[-16:])]
        atr = _mean(ranges)
        if not math.isfinite(atr) or atr <= 0:
            return {**base,"reason":"invalid_volatility"}
        close = bars[-1][4]
        # Retest > breakout > forming. All three use exclusively completed bars.
        for offset in (2,1,0):
            pattern = _flag_candidate(bars,offset,atr)
            if not pattern:
                continue
            sign = pattern.pop("direction_sign")
            trigger = pattern.pop("trigger")
            invalidation = pattern.pop("invalidation")
            result = {**base, "status":"watch", **pattern,
                      "trigger_usd":round(trigger,2),
                      "invalidation_usd":round(invalidation,2),
                      "last_completed_epoch":bars[-1][0]+60,
                      "candle_age_seconds":None}
            if offset == 2:
                breakout = bars[-2]
                last = bars[-1]
                if sign*(breakout[4]-trigger) > 0:
                    if sign*(last[4]-invalidation) <= 0:
                        result["phase"] = "INVALIDATED"
                    elif ((last[1] if sign>0 else last[2])*sign <= sign*(trigger+.25*sign*atr)
                          and sign*(last[4]-trigger) >= 0):
                        result["phase"] = "RETEST_HELD"
                    else:
                        continue
                    return result
            if offset == 1:
                if sign*(close-invalidation) <= 0:
                    result["phase"] = "INVALIDATED"
                    return result
                if sign*(close-trigger) > 0:
                    baseline = _mean([b[5] for b in bars[-16:-1]])
                    result["phase"] = "BREAKOUT_WATCH"
                    result["volume_confirmed"] = bool(baseline > 0 and bars[-1][5] >= baseline)
                    return result
            if offset == 0:
                result["phase"] = "FORMING"
                return result
        double = _double_turn(bars,atr)
        if double:
            return {**base, **double, "last_completed_epoch":bars[-1][0]+60}
        return {**base, "status":"monitoring", "reason":"no_eligible_setup",
                "last_completed_epoch":bars[-1][0]+60}
    except (TypeError, ValueError, IndexError, OverflowError):
        return {**base,"reason":"invalid_candles"}
