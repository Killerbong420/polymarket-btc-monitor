"""Forward-only BTC settlement-price scenarios, not fitted trading probabilities.

Target: the mean of one-second BRTI reference observations during the final
minute of the VERIFIED Polymarket US 15-minute round. Never mistake exchange
spot, current BRTI, or contract odds for that future settlement mean.
"""
import math
import statistics

VERSION = "brti-final-minute-projection-v1"


def finite(v):
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def clamp(x, lower, upper):
    return max(lower, min(upper, x))


def forecast_settlement(reference, external, target, seconds_left, now=None):
    base = {"status": "unavailable", "version": VERSION,
            "quantity": "future_final_60_second_BRTI_average",
            "source": "Polymarket US public BRTI reference",
            "probabilities_calibrated": False}
    target, left = finite(target), finite(seconds_left)
    if not isinstance(reference, dict) or not isinstance(external, dict):
        return {**base, "reason": "Missing reference or input data"}
    if reference.get("status") != "fresh":
        return {**base, "reason": "Fresh BRTI reference required; spot is not a substitute"}
    end = finite(reference.get("window_end"))
    if target is None or target <= 0 or left is None or not (0 < left <= 900) or end is None:
        return {**base, "reason": "Verified round time and official target required"}
    now = finite(now) if now is not None else end-left
    if now is None or abs((end-now)-left)>20:
        return {**base, "reason": "Stale or inconsistent market-clock snapshot"}
    observed = {}
    for item in reference.get("points") or []:
        try:
            timestamp, val = item
        except (ValueError, TypeError):
            continue
        timestamp, val = finite(timestamp), finite(val)
        if (timestamp is not None and val is not None and val > 0
                and abs(timestamp-round(timestamp))<0.001
                and end-960 <= timestamp <= min(now, end-1)):
            observed[int(round(timestamp))] = val
    if not observed:
        return {**base, "reason": "No timestamped published BRTI ticks"}
    seq = sorted(observed.items())
    last_t, price = seq[-1]
    age = now-last_t
    if age < -2 or age > 15:
        return {**base, "reason": "BRTI tick stale or future-dated", "age_seconds": round(age,1)}

    # Multi-horizon BRTI slope, using only observations available by now.
    speeds = []
    for horizon, weight in ((45, .50), (150, .32), (300, .18)):
        sample = [(t,p) for t,p in seq if last_t-horizon-8 <= t <= last_t-horizon]
        if sample:
            t0, p0 = sample[-1]
            if last_t > t0:
                speeds.append(((price-p0)*60/(last_t-t0), weight))
    trend_per_min = (sum(s*w for s,w in speeds)/sum(w for _,w in speeds)) if speeds else 0.

    # Volatility from actual reference changes; do not call this an
    # empirically calibrated interval. Sparse reference observations lower quality.
    per_minute = {}
    for t,p in seq:
        per_minute[t//60] = (t,p)
    sampled = sorted(per_minute.values())[-15:]
    changes = [(p1-p0)*60/(t1-t0)
               for (t0,p0),(t1,p1) in zip(sampled, sampled[1:])
               if 45 <= t1-t0 <= 90]
    volatility = max(price*.00012, statistics.pstdev(changes) if len(changes)>=3 else 0.0)
    max_trend = max(price*.00018, volatility*1.5)
    trend_per_min = clamp(trend_per_min, -max_trend, max_trend)
    contributions = {"brti_multi_horizon_usd_per_min": round(trend_per_min,4)}

    # Trend acceleration from two distinct, already observed BRTI intervals.
    recent = next(((t,p) for t,p in reversed(seq) if last_t-75 <= t <= last_t-50),None)
    prior = next(((t,p) for t,p in reversed(seq) if last_t-150 <= t <= last_t-110),None)
    if recent and prior and recent[0] > prior[0]:
        new_slope = (price-recent[1])*60/(last_t-recent[0])
        old_slope = (recent[1]-prior[1])*60/(recent[0]-prior[0])
        adjustment = clamp((new_slope-old_slope)*.15,-max_trend*.20,max_trend*.20)
        trend_per_min += adjustment
        contributions["brti_acceleration_usd_per_min"] = round(adjustment,4)

    # Fresh external flow and depth can gently adjust projected trajectory;
    # those signals are partial samples and may not cause a price move.
    age_candle, momentum = finite(external.get("candle_age_seconds")), finite(external.get("momentum_pct"))
    if age_candle is not None and 0 <= age_candle <= 75 and momentum is not None:
        per_min = price*clamp(momentum,-.5,.5)/100/3
        adjustment = clamp(per_min*.10,-max_trend*.10,max_trend*.10)
        trend_per_min += adjustment
        contributions["exchange_candle_usd_per_min"] = round(adjustment,4)
    intel = external.get("external_intelligence") or {}
    flow = intel.get("trade_flow") or {}
    pressure = finite(external.get("pressure"))
    if external.get("pressure_available") and flow.get("status")=="fresh" and pressure is not None:
        adjustment = clamp(pressure,-1,1)*max_trend*.07
        trend_per_min += adjustment
        contributions["sampled_trades_usd_per_min"] = round(adjustment,4)
    book = intel.get("order_book") or {}
    depth, depth_age = finite(book.get("imbalance")), finite(book.get("age_seconds"))
    if book.get("status")=="fresh" and depth is not None and depth_age is not None and 0 <= depth_age <= 30:
        adjustment = clamp(depth,-1,1)*max_trend*.05
        trend_per_min += adjustment
        contributions["kraken_book_usd_per_min"] = round(adjustment,4)
    trend_per_min = clamp(trend_per_min, -max_trend, max_trend)

    # A recent directional burst decays instead of being extrapolated linearly
    # across an entire round. This models a FUTURE path, not the current side.
    def estimate_at(t):
        horizon = max(0, t-last_t)
        effective_minutes = (180/60)*(1-math.exp(-horizon/180))
        return max(.01, price + trend_per_min*.8*effective_minutes)

    final_start, final_end = int(round(end))-60, int(round(end))
    actual_future = []
    missing_past = 0
    known = 0
    projected = 0
    for t in range(final_start, final_end):
        if t in observed:
            actual_future.append(observed[t])
            known += 1
        else:
            # A published gap is never counted as an observed BRTI tick.
            if t <= last_t:
                missing_past += 1
            actual_future.append(estimate_at(t))
            projected += 1
    price_mean = sum(actual_future)/60
    # Scenario width is heuristic and explicitly NOT a calibrated CI.
    horizon_m = max(0, min(900, end-now-30))/60
    half_range = max(price*.00003,
                     volatility*1.5*math.sqrt(max(.1,horizon_m))*(projected/60))
    down, up = max(.01,price_mean-half_range),price_mean+half_range
    quality = "LIMITED" if (not speeds or missing_past>2 or len(changes)<3) else "OBSERVED"
    return {**base, "status": "available", "quality": quality,
            "target_usd": round(target,2),
            "current_reference_usd": round(price,2),
            "forecast_final_mean_usd": round(price_mean,2),
            "forecast_delta_from_current_usd": round(price_mean-price,2),
            "expected_distance_to_target_usd": round(price_mean-target,2),
            "projected_direction": "UP" if price_mean>target else "DOWN" if price_mean<target else "TIE",
            "downside_scenario_usd": round(down,2),
            "upside_scenario_usd": round(up,2),
            "target_in_scenario_range": down<=target<=up,
            "volatility_reference_usd_per_min": round(volatility,3),
            "net_trend_usd_per_min": round(trend_per_min,4),
            "slope_inputs": len(speeds),
            "observed_final_minute_seconds": known,
            "modeled_final_minute_seconds": projected,
            "missing_past_final_minute_seconds": missing_past,
            "seconds_to_settlement": round(left,1),
            "age_seconds": round(age,1),
            "contributions": contributions,
            "limitations": "Forward scenario, not fitted/calibrated to settlement outcomes. Does not predict news or liquidity shocks. Any missing past BRTI ticks remain unobserved. Official settlement/outcome overrides estimates."}
