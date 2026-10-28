"""Forward-only matched research evaluation for guarded BRTI calls.

Grades the frozen candidate at each checkpoint with the later official winner
and settlement price. Older AI rows are never edited or relabeled.
"""
import json
import math


def _num(v):
    try:
        v=float(v)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError, OverflowError):
        return None


def evaluate_guarded(rows):
    groups={}
    seen=set()
    recent=[]
    for market, checkpoint, captured, snapshot_raw, calls_raw, winner, final_price in rows:
        try:
            snap=json.loads(snapshot_raw)
            calls=json.loads(calls_raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(snap,dict) or not isinstance(calls,dict):
            continue
        guarded=calls.get("GUARDED_PRIMARY") or {}
        if not isinstance(guarded,dict) or guarded.get("source")!="guarded-path-primary-v1":
            continue
        version=guarded["source"]
        key=(market,checkpoint,version)
        if key in seen:
            continue
        seen.add(key)
        group=groups.setdefault((checkpoint,version),{
            "checkpoint":checkpoint,"version":version,"calls":0,"abstained":0,
            "settled":0,"wins":0,"losses":0,
            "paired_ai":0,"guarded_paired_wins":0,"ai_paired_wins":0,
            "paired_path":0,"guarded_path_wins":0,"raw_path_wins":0,
            "price_pairs":0,"guarded_abs_error":0.0,"ai_abs_error":0.0,
            "path_abs_error":0.0,"priced_paper_trades":0,
            "hypothetical_net_usd":0.0,"cost_blocked":0
        })
        group["calls"]+=1
        direction=guarded.get("direction")
        if guarded.get("status")!="available" or direction not in ("UP","DOWN"):
            group["abstained"]+=1
            continue
        cost_review=guarded.get("entry_review") or {}
        if cost_review.get("action") in ("AVOID_HIGH_PRICE","INSUFFICIENT_MARGIN"):
            group["cost_blocked"]+=1
        if winner not in ("UP","DOWN"):
            continue
        group["settled"]+=1
        good=(direction==winner)
        group["wins"]+=int(good)
        group["losses"]+=int(not good)
        ai=calls.get("AI") or {}
        path=calls.get("PRICE_PATH") or {}
        if ai.get("source")=="AI_REVIEW" and ai.get("direction") in ("UP","DOWN"):
            group["paired_ai"]+=1
            group["guarded_paired_wins"]+=int(good)
            group["ai_paired_wins"]+=int(ai["direction"]==winner)
        if path.get("direction") in ("UP","DOWN"):
            group["paired_path"]+=1
            group["guarded_path_wins"]+=int(good)
            group["raw_path_wins"]+=int(path["direction"]==winner)
        final=_num(final_price)
        predicted=_num(guarded.get("forecast_final_mean_usd"))
        ai_value=_num(ai.get("forecast_final_mean_usd")) if ai.get("source")=="AI_REVIEW" else None
        path_value=_num(path.get("forecast_final_mean_usd"))
        if (final is not None and final>0 and predicted is not None
                and ai_value is not None and path_value is not None):
            group["price_pairs"]+=1
            group["guarded_abs_error"]+=abs(predicted-final)
            group["ai_abs_error"]+=abs(ai_value-final)
            group["path_abs_error"]+=abs(path_value-final)
        # This remains a hypothetical hold-to-settlement illustration, not a
        # trade fill or measured actual realized profit.
        if cost_review.get("action")=="MANUAL_REVIEW_ONLY":
            cost=cost_review.get("estimated_cost") or {}
            net=_num(cost.get("net_if_win_usd" if good else "net_if_loss_usd"))
            if net is not None and cost.get("status")=="estimated":
                group["hypothetical_net_usd"]+=net
                group["priced_paper_trades"]+=1
        if len(recent)<25:
            recent.append({"market":market,"checkpoint":checkpoint,"captured_at":captured,
                           "version":version,"guarded":direction,"ai":ai.get("direction"),
                           "path":path.get("direction"),"winner":winner,
                           "result":"WIN" if good else "LOSS",
                           "quality":guarded.get("confidence"),
                           "entry_review":cost_review.get("action")})

    output=[]
    for g in groups.values():
        n=g["settled"]; ai_n=g["paired_ai"]; path_n=g["paired_path"]; price_n=g["price_pairs"]
        output.append({
            "checkpoint":g["checkpoint"],"version":g["version"],
            "calls":g["calls"],"abstained":g["abstained"],
            "settled":n,"wins":g["wins"],"losses":g["losses"],
            "accuracy_pct":round(100*g["wins"]/n,1) if n else None,
            "paired_ai_rounds":ai_n,
            "guarded_vs_ai_pct":round(100*g["guarded_paired_wins"]/ai_n,1) if ai_n else None,
            "raw_ai_paired_pct":round(100*g["ai_paired_wins"]/ai_n,1) if ai_n else None,
            "paired_path_rounds":path_n,
            "guarded_vs_path_pct":round(100*g["guarded_path_wins"]/path_n,1) if path_n else None,
            "raw_path_paired_pct":round(100*g["raw_path_wins"]/path_n,1) if path_n else None,
            "paired_price_rounds":price_n,
            "guarded_mae_usd":round(g["guarded_abs_error"]/price_n,2) if price_n else None,
            "ai_mae_usd":round(g["ai_abs_error"]/price_n,2) if price_n else None,
            "raw_path_mae_usd":round(g["path_abs_error"]/price_n,2) if price_n else None,
            "cost_blocked":g["cost_blocked"],
            "hypothetical_review_entries":g["priced_paper_trades"],
            "hypothetical_hold_net_usd":round(g["hypothetical_net_usd"],2),
        })
    return {"status":"observed" if output else "collecting_forward_calls",
            "results":sorted(output,key=lambda x:(x["checkpoint"],x["version"])),
            "recent":recent,
            "limitations":"Versioned, prospectively captured calls only. Paired metrics require the same round/checkpoint. Quote examples are hypothetical held-to-settlement, not executed trades. No calibrated live odds or demonstrated future profitability. Research-only guarded decision does not overwrite the original AI call."}
