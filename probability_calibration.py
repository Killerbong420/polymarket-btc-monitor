"""Forward-only empirical probability calibration for 15-minute BTC rounds.

Never calibrate on the same market being predicted. No result is shown without
sufficient independent settled markets and chronological holdout validation.
"""
import math

MIN_TRAIN=120
MIN_HOLDOUT=40
MIN_BUCKET=30
BUCKETS=(-0.15,-0.05,0.0,0.05,0.15)

def bucket(gap_pct):
    if gap_pct is None or not math.isfinite(float(gap_pct)):
        return None
    return sum(float(gap_pct)>edge for edge in BUCKETS)

def calibrate(rows, current_gap_pct, current_market=None):
    """Rows: chronological (market, observed_gap_pct, official_winner).

    Returns unavailable until chronological held-out Brier score beats a
    constant-frequency baseline. Use only exact official settled outcomes.
    """
    b=bucket(current_gap_pct)
    if b is None:return {"status":"unavailable","reason":"missing_verified_gap"}
    clean=[]
    seen=set()
    for market,gap,winner in rows:
        if not market or market==current_market or market in seen or winner not in ("UP","DOWN"):
            continue
        k=bucket(gap)
        if k is None:continue
        seen.add(market)
        clean.append((k,1 if winner=="UP" else 0))
    if len(clean)<MIN_TRAIN+MIN_HOLDOUT:
        return {"status":"unavailable","reason":"insufficient_settled_rounds","samples":len(clean)}
    train,holdout=clean[:-MIN_HOLDOUT],clean[-MIN_HOLDOUT:]
    def estimate(data,k):
        selected=[y for group,y in data if group==k]
        if len(selected)<MIN_BUCKET:return None
        # Shrink sparse bins toward the training base rate.
        prior=(sum(y for _,y in data)+1)/(len(data)+2)
        return (sum(selected)+20*prior)/(len(selected)+20)
    preds=[estimate(train,k) for k,_ in holdout]
    if any(p is None for p in preds):
        return {"status":"unavailable","reason":"insufficient_bucket_coverage"}
    baseline=(sum(y for _,y in train)+1)/(len(train)+2)
    model_brier=sum((p-y)**2 for p,(_,y) in zip(preds,holdout))/len(holdout)
    base_brier=sum((baseline-y)**2 for _,y in holdout)/len(holdout)
    if model_brier>=base_brier:
        return {"status":"unavailable","reason":"holdout_not_better_than_baseline",
                "model_brier":round(model_brier,4),"baseline_brier":round(base_brier,4)}
    p=estimate(clean,b)
    if p is None:return {"status":"unavailable","reason":"insufficient_current_bucket"}
    return {"status":"available","up_probability":round(p,4),
            "down_probability":round(1-p,4),"samples":len(clean),
            "holdout":len(holdout),"model_brier":round(model_brier,4),
            "baseline_brier":round(base_brier,4),
            "method":"chronological_empirical_gap_bins_v1",
            "note":"Historical conditional frequency, not a guarantee; changing markets can invalidate calibration."}
