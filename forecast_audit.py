"""Forward-only settlement forecast diagnostics, grouped by model/code version.

This file does not trade, tune the model or mutate predictions. One market
checkpoint is one observation; the same round at other checkpoints is NOT an
independent new outcome. Numeric errors require published final BRTI means.
"""
import json
import math


def finite(value):
    try:
        v = float(value)
        return v if math.isfinite(v) and v > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def summarize_forecast_audit(rows):
    groups = {}
    seen = set()
    ignored = 0
    for row in rows:
        if len(row) != 7:
            ignored += 1
            continue
        market, checkpoint, created, raw_snapshot, raw_calls, winner, actual = row
        try:
            snap, calls = json.loads(raw_snapshot), json.loads(raw_calls)
        except (ValueError, TypeError):
            ignored += 1
            continue
        if not isinstance(snap, dict) or not isinstance(calls, dict):
            ignored += 1
            continue
        version = str(snap.get("version") or "UNKNOWN")
        model = str(snap.get("ai_model") or "UNKNOWN")
        unique = (market, checkpoint, version)
        if unique in seen:
            ignored += 1
            continue
        seen.add(unique)
        key = (checkpoint, version, model)
        g = groups.setdefault(key, {
            "checkpoint": checkpoint, "version": version, "model": model,
            "ai_calls": 0, "settled_ai_calls": 0, "ai_wins": 0,
            "direction_only_count": 0,
            "baseline_comparable_rounds": 0, "baseline_wins": 0,
            "ai_wins_on_paired_rounds": 0,
            "ai_reverse_calls": 0, "successful_reversal_calls": 0,
            "false_reversal_calls": 0, "missed_reversals": 0,
            "identical_price_pairs": 0, "price_path_comparable_rounds": 0,
            "numeric_ai_scored_rounds": 0, "numeric_ai_abs_error_usd": 0.0,
            "numeric_ai_signed_error_usd": 0.0,
            "price_path_scored_rounds": 0, "price_path_abs_error_usd": 0.0,
            "current_reference_scored_rounds": 0, "current_reference_abs_error_usd": 0.0,
            "paired_numeric_rounds": 0, "paired_ai_abs_error_usd": 0.0,
            "paired_path_abs_error_usd": 0.0, "paired_static_abs_error_usd": 0.0,
            "independent_forecast_calls": 0
        })
        ai = calls.get("AI") or {}
        if not isinstance(ai, dict) or ai.get("source") != "AI_REVIEW":
            continue
        direction = ai.get("direction")
        if direction not in ("UP", "DOWN"):
            continue
        g["ai_calls"] += 1
        if ai.get("independent_forecast") is True:
            g["independent_forecast_calls"] += 1
        if ai.get("forecast_status") == "DIRECTION_ONLY":
            g["direction_only_count"] += 1
        path = calls.get("PRICE_PATH") or {}
        path = path if isinstance(path, dict) else {}
        ai_price = finite(ai.get("forecast_final_mean_usd"))
        path_price = finite(path.get("forecast_final_mean_usd"))
        if ai_price is not None and path_price is not None:
            g["price_path_comparable_rounds"] += 1
            if round(ai_price, 2) == round(path_price, 2):
                g["identical_price_pairs"] += 1
        if winner not in ("UP", "DOWN"):
            continue
        g["settled_ai_calls"] += 1
        g["ai_wins"] += int(direction == winner)
        base = calls.get("TARGET_SIDE") or {}
        base_dir = base.get("direction") if isinstance(base, dict) else None
        if base_dir in ("UP", "DOWN"):
            g["baseline_comparable_rounds"] += 1
            g["baseline_wins"] += int(base_dir == winner)
            g["ai_wins_on_paired_rounds"] += int(direction == winner)
            if direction != base_dir:
                g["ai_reverse_calls"] += 1
                if direction == winner:
                    g["successful_reversal_calls"] += 1
                else:
                    g["false_reversal_calls"] += 1
            elif winner != base_dir:
                g["missed_reversals"] += 1
        actual = finite(actual)
        static = finite((snap.get("settlement_forecast") or {}).get("current_reference_usd"))
        if actual is None:
            continue
        if ai_price is not None:
            g["numeric_ai_scored_rounds"] += 1
            g["numeric_ai_abs_error_usd"] += abs(ai_price - actual)
            g["numeric_ai_signed_error_usd"] += ai_price - actual
        if path_price is not None:
            g["price_path_scored_rounds"] += 1
            g["price_path_abs_error_usd"] += abs(path_price - actual)
        if static is not None:
            g["current_reference_scored_rounds"] += 1
            g["current_reference_abs_error_usd"] += abs(static - actual)
        if ai_price is not None and path_price is not None and static is not None:
            g["paired_numeric_rounds"] += 1
            g["paired_ai_abs_error_usd"] += abs(ai_price - actual)
            g["paired_path_abs_error_usd"] += abs(path_price - actual)
            g["paired_static_abs_error_usd"] += abs(static - actual)
    output = []
    for g in groups.values():
        n = g["settled_ai_calls"]
        paired = g["baseline_comparable_rounds"]
        numeric = g["paired_numeric_rounds"]
        g["ai_accuracy_pct"] = round(100 * g["ai_wins"] / n, 1) if n else None
        g["paired_ai_accuracy_pct"] = round(100*g["ai_wins_on_paired_rounds"]/paired, 1) if paired else None
        g["baseline_accuracy_pct"] = round(100*g["baseline_wins"]/paired, 1) if paired else None
        g["ai_advantage_percentage_points"] = (
            round(100*(g["ai_wins_on_paired_rounds"]-g["baseline_wins"])/paired,1) if paired else None)
        g["reversal_success_pct"] = (
            round(100*g["successful_reversal_calls"]/g["ai_reverse_calls"],1)
            if g["ai_reverse_calls"] else None)
        g["identical_price_forecast_pct"] = (
            round(100*g["identical_price_pairs"]/g["price_path_comparable_rounds"],1)
            if g["price_path_comparable_rounds"] else None)
        for label, count, total in [
            ("ai_mae_usd", "numeric_ai_scored_rounds", "numeric_ai_abs_error_usd"),
            ("path_mae_usd", "price_path_scored_rounds", "price_path_abs_error_usd"),
            ("static_mae_usd", "current_reference_scored_rounds", "current_reference_abs_error_usd")]:
            g[label] = round(g[total]/g[count],2) if g[count] else None
            del g[total]
        g["ai_mean_signed_error_usd"] = (
            round(g["numeric_ai_signed_error_usd"]/g["numeric_ai_scored_rounds"],2)
            if g["numeric_ai_scored_rounds"] else None)
        del g["numeric_ai_signed_error_usd"]
        if numeric:
            g["paired_ai_mae_usd"] = round(g["paired_ai_abs_error_usd"]/numeric,2)
            g["paired_path_mae_usd"] = round(g["paired_path_abs_error_usd"]/numeric,2)
            g["paired_unchanged_mae_usd"] = round(g["paired_static_abs_error_usd"]/numeric,2)
        else:
            g["paired_ai_mae_usd"] = g["paired_path_mae_usd"] = g["paired_unchanged_mae_usd"] = None
        del g["paired_ai_abs_error_usd"]
        del g["paired_path_abs_error_usd"]
        del g["paired_static_abs_error_usd"]
        g["eligible_to_consider_training"] = n >= 300
        g["sample_note"] = ("SMALL SAMPLE; do not optimize on these outcomes"
                            if n < 300 else "Training may be evaluated with chronological holdouts")
        output.append(g)
    order = {"EARLY": 0, "HALFWAY": 1, "LATE": 2}
    output.sort(key=lambda x:(order.get(x["checkpoint"],9),x["version"],x["model"]))
    return {
        "status": "observed" if output else "awaiting_research_calls",
        "version": "forward-forecast-independent-audit-v1",
        "groups": output, "ignored_rows": ignored,
        "training_gate": "300+ SETTLED rounds at the same checkpoint and model/code version; walk-forward evaluation required before any promotion.",
        "limitations": "One round is repeated at three checkpoints; do not pool them as independent outcomes. Reversal means AI disagreed with the observed target side, not that a within-round price reversal was proven. Accuracy is not trading P&L; missing official BRTI prices are excluded."
    }
