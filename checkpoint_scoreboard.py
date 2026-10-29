"""Transparent, strictly out-of-sample checkpoint score summaries.

Only frozen historical calls with an officially verified winner are graded.
No near-expiry synthetic backfill, no implied guarantee, and no cross-model pooling.
"""
import json

CHECKPOINTS=(("EARLY","12:00 left"),("HALFWAY","7:30 left"),("LATE","3:00 left"))
METHODS=(("AI","Independent AI"),("PRICE_PATH","BRTI price path"))

def summarize_checkpoint_rows(rows, limit=100):
    grouped={}
    for checkpoint,label in CHECKPOINTS:
        for method,name in METHODS:
            grouped[(checkpoint,method)]={"checkpoint":checkpoint,"label":label,"method":method,
                "name":name,"wins":0,"losses":0,"graded":0,"accuracy_pct":None,
                "excluded_no_call":0,"sample_basis":"Last 100 independently frozen, officially settled calls or fewer"}
    seen=set()
    for market,checkpoint,raw_calls,winner in rows:
        if winner not in ("UP","DOWN") or checkpoint not in dict(CHECKPOINTS):continue
        try:calls=json.loads(raw_calls)
        except (TypeError,ValueError,AttributeError):continue
        if not isinstance(calls,dict):continue
        for method,_ in METHODS:
            key=(market,checkpoint,method)
            if key in seen:continue
            seen.add(key)
            group=grouped[(checkpoint,method)]
            if group["graded"]>=limit:continue
            call=calls.get(method) or {}
            direction=call.get("direction") if isinstance(call,dict) else None
            eligible=direction in ("UP","DOWN") and (method!="AI" or call.get("source")=="AI_REVIEW")
            if not eligible:
                group["excluded_no_call"]+=1
                continue
            group["wins" if direction==winner else "losses"]+=1
            group["graded"]+=1
    for item in grouped.values():
        if item["graded"]:
            item["accuracy_pct"]=round(item["wins"]/item["graded"]*100,1)
    return [grouped[(checkpoint,method)] for checkpoint,_ in CHECKPOINTS for method,_ in METHODS]

def build_scoreboard(rows,five_report=None):
    result=summarize_checkpoint_rows(rows)
    five_report=five_report or {}
    def five_item(key,label,stats):
        wins=int(stats.get("wins") or 0)
        losses=int(stats.get("losses") or 0)
        n=wins+losses
        return {"market":"5m","checkpoint":key,"label":label,
                "name":"Frozen BRTI forecast","wins":wins,"losses":losses,
                "graded":n,"accuracy_pct":round(wins/n*100,1) if n else None,
                "basis":"Verified settlements only, no retroactive checkpoint assignment"}
    five=[five_item("EARLY","First available",five_report),
          five_item("LATE_2M30","2:30 left · strict",five_report.get("late") or {})]
    return {"status":"available","fifteen":result,"five":five,"window_size":100,
            "limitations":"Different checkpoints have different samples and information. Accuracy improves naturally as expiry approaches; no profitability, calibration, or prediction guarantee is inferred.",
            "late_audit":(five_report.get("late") or {}).get("capture_audit")}
