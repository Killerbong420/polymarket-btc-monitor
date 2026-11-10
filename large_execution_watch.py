"""Read-only, coverage-gated watch for unusually large BTC/USD executions.

A 'whale-sized' execution is an observable large AGGRESSIVE trade, not a
confirmed whale identity, owner, coordinated order, or price forecast.
Uses only the existing bounded Coinbase/Kraken websocket buffers and the
already-fetched Kraken depth snapshot; no new external requests.
"""
import math
import statistics

VERSION = "large-execution-watch-v1"
BASELINE_MIN_SAMPLES = 40
FLOOR_USD = 250_000
WINDOW_SECONDS = 30


def _amount(value):
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _percentile(sorted_values, fraction):
    idx = min(len(sorted_values)-1, max(0, int((len(sorted_values)-1)*fraction)))
    return sorted_values[idx]


def examine_stream(sample, source, now):
    """Only a warmed, fresh, non-overflowed stream can trigger a live watch.

    Baseline is older observed executions (35–150 seconds ago), excluding the
    active 30s test window to avoid leakage from the event being detected.
    """
    out = {"source": source, "status": "collecting",
           "reason": "insufficient_stream_coverage",
           "sample_window_seconds": WINDOW_SECONDS,
           "threshold_method": "max($250k, 8x historical median, 2.5x historical p95)",
           "minimum_usd": FLOOR_USD, "version": VERSION}
    if source not in ("Coinbase", "Kraken") or not isinstance(sample, dict):
        return {**out, "status": "unavailable", "reason": "live_execution_stream_required"}
    connected = _amount(sample.get("connected_since_epoch"))
    latest = _amount(sample.get("latest_trade_epoch"))
    overflow = _amount(sample.get("overflow_epoch"))
    if connected is None or latest is None or connected > now or latest > now+2 or now-latest > 5:
        return {**out, "status": "unavailable", "reason": "stream_disconnected_or_stale"}
    if now-connected < 151:
        return {**out, "reason": "baseline_warming"}
    if overflow is not None and now-overflow <= 155:
        return {**out, "status": "partial", "reason": "buffer_overflow_or_missing_executions"}
    data = sample.get("rows")
    if not isinstance(data, list):
        return {**out, "status": "unavailable", "reason": "invalid_sample"}
    base, recent = [], []
    for entry in data:
        if not isinstance(entry, (list, tuple)) or len(entry) < 4:
            continue
        stamp, price, qty = (_amount(entry[i]) for i in range(3))
        side = entry[3]
        if (stamp is None or price is None or qty is None or
                price <= 0 or qty <= 0 or side not in ("buy", "sell") or
                stamp > now+2 or stamp < now-180):
            continue
        notional = price * qty
        if not math.isfinite(notional):
            continue
        if now-150 <= stamp <= now-35:
            base.append((stamp, notional))
        elif now-30 <= stamp <= now:
            recent.append((stamp, notional, side))
    if (len(base) < BASELINE_MIN_SAMPLES or
            min((x[0] for x in base), default=now) > now-125 or
            max((x[0] for x in base), default=0) < now-42):
        return {**out, "reason": "historical_sample_too_short"}
    if (not recent or min(x[0] for x in recent) > now-27
            or max(x[0] for x in recent) < now-5):
        return {**out, "reason": "recent_execution_window_incomplete"}
    baseline = sorted(item[1] for item in base)
    threshold = max(FLOOR_USD, 8*statistics.median(baseline),
                    2.5*_percentile(baseline,.95))
    large = [item for item in recent if item[1]>=threshold]
    buy = sum(v for _,v,side in large if side=="buy")
    sell = sum(v for _,v,side in large if side=="sell")
    biggest = max(large, key=lambda row:row[1]) if large else None
    side = ("BUY_SIDE" if buy > sell*1.5 else
            "SELL_SIDE" if sell > buy*1.5 else
            "MIXED") if large else "NONE"
    return {**out,
            "status": "watch" if large else "monitoring",
            "reason": "large_aggressive_execution_observed" if large else "no_unusually_large_executions",
            "side": side,
            "threshold_usd": round(threshold, 0),
            "observed_execution_count": len(recent),
            "baseline_count":len(base),
            "large_execution_count":len(large),
            "large_buy_usd":round(buy, 0),
            "large_sell_usd":round(sell, 0),
            "largest_execution_usd":round(biggest[1], 0) if biggest else None,
            "largest_execution_side":biggest[2] if biggest else None,
            "latest_trade_age_seconds":round(max(0,now-latest), 1),
            "observation_only": True}


def combine_watches(flow, book, now):
    """Aggregate qualified VENUE executions, never infer an account identity."""
    out={"status":"collecting","version":VERSION,
         "label":"Unusually large aggressive BTC/USD executions",
         "not_whale_identity":True,"changes_primary_prediction":False,
         "probability_calibrated":False,
         "note":"Exchange executions reveal aggressive buy/sell sides, not trader identity. "
                "Partial exchange coverage; orders in a book are not executed trades.",
         "as_of_epoch":now}
    if not isinstance(flow,dict) or flow.get("status")!="available":
        return {**out,"status":"unavailable","reason":"trade_stream_unavailable"}
    rows=flow.get("exchanges")
    if not isinstance(rows,list):
        return {**out,"status":"unavailable","reason":"no_exchange_samples"}
    qualified=[]
    for entry in rows:
        if not isinstance(entry,dict) or entry.get("status")!="fresh" or entry.get("transport")!="websocket":
            continue
        watch=entry.get("large_trade_watch")
        if isinstance(watch,dict) and watch.get("status") in ("watch","monitoring"):
            qualified.append(watch)
    if not qualified:
        return {**out,"status":"collecting","reason":"waiting_for_warmed_live_execution_samples"}
    flagged=[x for x in qualified if x["status"]=="watch"]
    buy=sum(x.get("large_buy_usd") or 0 for x in flagged)
    sell=sum(x.get("large_sell_usd") or 0 for x in flagged)
    side=("BUY_SIDE" if buy>1.5*sell else
          "SELL_SIDE" if sell>1.5*buy else "MIXED") if flagged else "NONE"
    result={**out,"status":"watch" if flagged else "monitoring",
            "side":side,"eligible_venues":[x["source"] for x in qualified],
            "large_execution_count":sum(x.get("large_execution_count",0) for x in flagged),
            "large_buy_usd":round(buy,0),"large_sell_usd":round(sell,0),
            "largest_execution_usd":max((x.get("largest_execution_usd") or 0 for x in flagged),default=0),
            "venue_watches":qualified}
    if (isinstance(book,dict) and book.get("status")=="fresh"
            and book.get("source")=="Kraken BTC/USD"):
        at=_amount(book.get("fetched_epoch"))
        im=_amount(book.get("imbalance"))
        if at is not None and -3 <= now-at <= 35 and im is not None and -1<=im<=1:
            result["resting_liquidity_imbalance_pct"]=round(100*im,1)
            result["book_note"]="Kraken 20-level displayed liquidity only; not executed whale activity"
    return result
