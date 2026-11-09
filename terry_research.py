"""Independent read-only research on TerryCalls' 15-minute published history.

All outcomes here are provider-reported, not an independent settlement audit.
Reported entry quotes are NOT fills. No data from this module influences trades,
live predictions, or model training. Each historic market is counted once.
"""
import math
from datetime import datetime, timezone

FEE_COEFFICIENT = 0.0695  # illustrative standard US taker fee, not a fill quote
VERSION = "terry-study-v1"


def probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0 <= value <= 1 else None


def side(value):
    result = str(value or "").upper()
    return result if result in ("UP", "DOWN") else None


def epoch(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).timestamp() if dt.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def normalize(window):
    """Whitelist a settled 15m provider window; never archive unknown fields."""
    if not isinstance(window, dict) or window.get("horizon") != "15m":
        return None
    market = window.get("market")
    result = side(window.get("result"))
    if (not isinstance(market, str) or not market.startswith("cpc-btc-updown-15m-")
            or len(market) > 100 or result is None):
        return None
    parsed = {"market": market, "horizon": "15m", "result": result}
    for kind in ("call", "lock"):
        raw = window.get(kind) if isinstance(window.get(kind), dict) else {}
        direction = side(raw.get("side"))
        chance = probability(raw.get("chance"))
        price = probability(raw.get("price"))
        rule = str(raw.get("rule") or "UNKNOWN")[:80]
        seconds_in = raw.get("seconds_in")
        if (isinstance(seconds_in, bool) or not isinstance(seconds_in, (int, float))
                or not math.isfinite(seconds_in) or not 0 <= seconds_in <= 900):
            end, at = epoch(window.get("window_end")), epoch(raw.get("at"))
            seconds_in = 900 - (end - at) if end is not None and at is not None else None
        parsed[kind] = {
            "side": direction, "chance": chance, "price": price,
            "rule": rule, "seconds_in": round(seconds_in) if seconds_in is not None and 0 <= seconds_in <= 900 else None,
        } if direction else None
    return parsed


def _average(values, digits=2):
    return round(sum(values) / len(values), digits) if values else None


def _evaluated(calls):
    n = len(calls)
    if not n:
        return {"settled": 0, "accuracy_pct": None}
    wins = sum(1 for c in calls if c["side"] == c["result"])
    confidence_rows = [c for c in calls if c["chance"] is not None]
    prices = [c for c in calls if c["price"] is not None and 0 < c["price"] < 1]
    briers = [(c["chance"] - int(c["side"] == c["result"])) ** 2 for c in confidence_rows]
    estimated_net = [
        int(c["side"] == c["result"]) - c["price"]
        - FEE_COEFFICIENT * c["price"] * (1 - c["price"]) for c in prices
    ]
    return {
        "settled": n, "right": wins, "accuracy_pct": round(100 * wins / n, 1),
        "probability_samples": len(confidence_rows),
        "mean_stated_confidence_pct": (
            _average([100 * c["chance"] for c in confidence_rows], 1)),
        "brier_score": _average(briers, 4),
        "quoted_entry_samples": len(prices),
        "hypothetical_taker_net_one_contract_each_usd": round(sum(estimated_net), 3),
        "hypothetical_mean_net_per_quoted_contract_usd": _average(estimated_net, 4),
    }


def _group(calls, classifier, limit=12):
    grouped = {}
    for call in calls:
        label = classifier(call)
        if label is not None:
            grouped.setdefault(label, []).append(call)
    return [
        {"group": name, **_evaluated(grouped[name])}
        for name in sorted(grouped, key=lambda name: (-len(grouped[name]), name))[:limit]
    ]


def _timing(c):
    if c["seconds_in"] is None:
        return "UNKNOWN"
    remaining = 900 - c["seconds_in"]
    return "EARLY (>7.5m left)" if remaining > 450 else (
        "LATE (<2m left)" if remaining < 120 else "MIDDLE (2-7.5m left)")


def _price(c):
    price = c["price"]
    if price is None:
        return "UNQUOTED"
    if price < .37:
        return "UNDERDOG (<37c)"
    if price < .63:
        return "MIDDLE (37-63c)"
    if price <= .92:
        return "FAVORITE (63-92c)"
    return "VERY_EXPENSIVE (>92c)"


def _confidence(c):
    p = c["chance"]
    if p is None:
        return None
    bucket = min(9, int(p * 10))
    return f"{bucket * 10}-{(bucket + 1) * 10}%"


def summarize_history(windows):
    """Describe provider history without fitting or modifying the live model."""
    by_market = {}
    for window in windows:
        record = normalize(window)
        if record is not None:
            by_market[record["market"]] = record
    data = list(by_market.values())
    calls, locks = [], []
    for record in data:
        for kind, target in (("call", calls), ("lock", locks)):
            details = record[kind]
            if details:
                target.append({**details, "result": record["result"]})
    return {
        "version": VERSION, "status": "observed" if data else "collecting",
        "settled_windows": len(data), "called_windows": len(calls),
        "call_coverage_pct": round(100 * len(calls) / len(data), 1) if data else None,
        "calls": _evaluated(calls), "locks_separate": _evaluated(locks),
        "calls_by_rule": _group(calls, lambda c: c["rule"]),
        "calls_by_entry_price": _group(calls, _price),
        "calls_by_timing": _group(calls, _timing),
        "confidence_calibration": _group(calls, _confidence),
        "sample_warning": "Small or selected samples do not establish a repeatable edge.",
        "limitations": (
            "TerryCalls provider-reported settled windows only; excludes pending and "
            "uncalled markets from direction accuracy. Quoted entries are not fills; "
            "hypothetical taker net assumes one contract per call, no slippage, "
            "full settlement, and a published fee formula. Not actual profit. "
            "Call and lock rules are scored separately; versions are not conflated. "
            "No independent Polymarket settlement verification or causal comparison "
            "to our bot. No automatic prediction or trade changes."
        ),
    }


def summarize_forward(samples):
    """Fixed one-sample-per-market-per-minute evaluation of live probabilities.

    samples: iterable of (market, minute_slot, up_probability, our_side,
    official_or_provider_result). Samples MUST have been captured before the
    settlement and paired with the same market, never reconstructed later.
    """
    seen = set()
    buckets = {}
    for market, slot, up, ours, result in samples:
        if (not isinstance(market, str) or isinstance(slot, bool)
                or not isinstance(slot, int) or not 0 <= slot < 15):
            continue
        chance, winner = probability(up), side(result)
        if chance is None or winner is None or (market, slot) in seen:
            continue
        seen.add((market, slot))
        row = buckets.setdefault(slot, [])
        prediction = "UP" if chance > .5 else ("DOWN" if chance < .5 else None)
        row.append((chance, winner, prediction, side(ours)))
    result = []
    for slot in sorted(buckets):
        rows = buckets[slot]
        terry_predictions = [r for r in rows if r[2]]
        matched = [r for r in rows if r[2] and r[3]]
        result.append({
            "market_minute": slot + 1, "settled_samples": len(rows),
            "terry_probability_brier": _average([
                (p - int(winner == "UP")) ** 2 for p, winner, _, _ in rows], 4),
            "terry_direction_accuracy_pct": _average([
                100 * int(predicted == winner) for _, winner, predicted, _ in terry_predictions], 1),
            "paired_rounds": len(matched),
            "terry_paired_accuracy_pct": _average([
                100 * int(predicted == winner) for _, winner, predicted, _ in matched], 1),
            "ours_paired_accuracy_pct": _average([
                100 * int(ours == winner) for _, winner, _, ours in matched], 1),
        })
    return {
        "version": VERSION, "status": "observed" if seen else "collecting",
        "checkpoints": result,
        "limitations": "Prospectively captured, same-market, same-minute samples. Each minute independently samples markets; repeated markets across minutes are correlated. Terry is scored at the sampled chance_now, not the earlier original call. Ours is a directional baseline without calibrated probability. Source outcomes must be independently verified before treating this as official accuracy.",
    }
