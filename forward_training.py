"""Read-only, leakage-resistant settlement training laboratory.

Never fits a live model on a small dataset. Only VERIFIED published 60-second
BRTI final means are eligible. Train/validation are strictly chronological
within a single checkpoint and research-version cohort. Models are never
applied to active rounds by this module.
"""
import json
import math
from collections import defaultdict

MIN_COMPLETE_ROUNDS = 300


def finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError,OverflowError):
        return None


def make_example(row):
    """One frozen forward checkpoint paired with its delayed official outcome."""
    if len(row)!=7:
        return None
    market, checkpoint, created, snapshot, calls, winner, actual = row
    if winner not in ("UP","DOWN"):
        return None
    try:
        snap=json.loads(snapshot)
        calls=json.loads(calls)
    except (TypeError,ValueError):
        return None
    if not isinstance(snap,dict) or not isinstance(calls,dict):
        return None
    ref=snap.get("reference_price") or {}
    path=snap.get("settlement_forecast") or {}
    chart=snap.get("chart_pattern") or {}
    if not all(isinstance(x,dict) for x in (ref,path,chart)):
        return None
    if ref.get("source")!="Polymarket US BTC reference / BRTI" or ref.get("status")!="fresh":
        return None
    current=finite(ref.get("price"))
    opening=finite(snap.get("official_target"))
    final=finite(actual)
    baseline=finite(path.get("forecast_final_mean_usd"))
    left=finite(snap.get("seconds_left"))
    if None in (current,opening,final,baseline,left) or min(current,opening,final,baseline)<=0:
        return None
    if not 0<left<=900:
        return None
    # Scaled basis-point features avoid price-level overfitting to old BTC regimes.
    diff=10000*(baseline/current-1)
    target_gap=10000*(opening/current-1)
    trend=finite(path.get("net_trend_usd_per_min")) or 0.0
    trend_bps=10000*trend/current
    momentum=(finite(chart.get("momentum_3m_pct")) or 0.0)*100
    reversal=float(bool(path.get("opposing_short_long_slopes")))
    x=[diff,target_gap,trend_bps,momentum,reversal]
    y=10000*(final/current-1)
    return {"market":str(market),"checkpoint":str(checkpoint),
            "version":str(snap.get("version") or "UNKNOWN"),
            "created":str(created),"x":x,"y":y,
            "current":current,"target":opening,"actual":final,
            "base":baseline,"winner":winner}


def _solve(matrix, vector):
    """Pivoted Gaussian elimination for a small ridge-normal system."""
    n=len(vector)
    a=[list(row)+[float(vector[i])] for i,row in enumerate(matrix)]
    for k in range(n):
        pivot=max(range(k,n),key=lambda i:abs(a[i][k]))
        if abs(a[pivot][k])<1e-9:
            raise ValueError("Ill-conditioned regression")
        a[k],a[pivot]=a[pivot],a[k]
        div=a[k][k]
        a[k]=[v/div for v in a[k]]
        for i in range(n):
            if i==k:continue
            factor=a[i][k]
            a[i]=[a[i][j]-factor*a[k][j] for j in range(n+1)]
    return [a[i][-1] for i in range(n)]


def train_ridge(train, alpha=25.0):
    if len(train)<15:
        raise ValueError("Insufficient training examples")
    cols=list(zip(*(item["x"] for item in train)))
    means=[sum(c)/len(c) for c in cols]
    scales=[max(1e-5,(sum((v-m)**2 for v in c)/len(c))**.5)
            for c,m in zip(cols,means)]
    features=[[1.0]+[(x-m)/s for x,m,s in zip(item["x"],means,scales)]
              for item in train]
    y=[item["y"] for item in train]
    dim=len(features[0])
    a=[[sum(row[i]*row[j] for row in features)+(alpha if i==j and i>0 else 0)
        for j in range(dim)] for i in range(dim)]
    b=[sum(row[i]*outcome for row,outcome in zip(features,y)) for i in range(dim)]
    coefs=_solve(a,b)
    return means,scales,coefs


def predict_bps(trained, features):
    means,scales,coefs=trained
    raw=coefs[0]+sum(coefs[i+1]*(x-means[i])/scales[i]
                     for i,x in enumerate(features))
    return max(-500.0,min(500.0,raw))


def validate_cohort(examples):
    """Return holdout metrics; never include validation outcomes in training."""
    ordered=sorted(examples,key=lambda e:(e["created"],e["market"]))
    n=len(ordered)
    status={"graded_complete_rounds":n, "required_complete_rounds":MIN_COMPLETE_ROUNDS,
            "first_captured_at":ordered[0]["created"] if ordered else None,
            "last_captured_at":ordered[-1]["created"] if ordered else None}
    if n<MIN_COMPLETE_ROUNDS:
        return {**status,"status":"collecting","shortfall":MIN_COMPLETE_ROUNDS-n,
                "live_model_unchanged":True}
    cutoff=int(n*.70)
    train=ordered[:cutoff]
    validation=ordered[cutoff:]
    model=train_ridge(train)
    stats={"candidate_error":0.0,"path_error":0.0,"static_error":0.0,
           "candidate_correct":0,"path_correct":0,"static_correct":0}
    for item in validation:
        predicted=item["current"]*(1+predict_bps(model,item["x"])/10000)
        current=item["current"];target=item["target"];winner=item["winner"]
        stats["candidate_error"]+=abs(predicted-item["actual"])
        stats["path_error"]+=abs(item["base"]-item["actual"])
        stats["static_error"]+=abs(current-item["actual"])
        stats["candidate_correct"]+=int(("UP" if predicted>=target else "DOWN")==winner)
        stats["path_correct"]+=int(("UP" if item["base"]>=target else "DOWN")==winner)
        stats["static_correct"]+=int(("UP" if current>=target else "DOWN")==winner)
    v=len(validation)
    performance={
        "holdout_rounds":v, "train_rounds":len(train), "split":"strict_chronological_70_30",
        "candidate_mae_usd":round(stats["candidate_error"]/v,2),
        "path_mae_usd":round(stats["path_error"]/v,2),
        "unchanged_mae_usd":round(stats["static_error"]/v,2),
        "candidate_accuracy_pct":round(100*stats["candidate_correct"]/v,1),
        "path_accuracy_pct":round(100*stats["path_correct"]/v,1),
        "unchanged_accuracy_pct":round(100*stats["static_correct"]/v,1)}
    # Directional calls are the actual decision target. Require a meaningful
    # improvement on untouched chronological holdout data, not merely a tiny
    # one-round advantage that could be noise. Early checkpoints are especially
    # noisy, so demand at least five additional correct settled rounds.
    required_direction_gain = max(5, math.ceil(0.03 * v)) if ordered[0]["checkpoint"] == "EARLY" else max(3, math.ceil(0.02 * v))
    direction_gain = stats["candidate_correct"] - max(stats["path_correct"], stats["static_correct"])
    improves=(stats["candidate_error"]<.98*min(stats["path_error"],stats["static_error"]) and
              direction_gain>=required_direction_gain)
    return {**status,"status":"validated_candidate" if improves else "no_verified_improvement",
            "direction_gain_vs_best_baseline":direction_gain,
            "required_direction_gain":required_direction_gain,
            "evaluation":performance, "passes_offline_gate":bool(improves),
            "live_model_unchanged":True,
            "note":"This single chronological holdout is a screening test, not proof of live predictive edge."}


def training_readiness(rows):
    grouped=defaultdict(dict)
    for row in rows:
        e=make_example(row)
        if e is None:continue
        grouped[(e["checkpoint"],e["version"])].setdefault(e["market"],e)
    cohorts=[]
    for (checkpoint,version),rounds in sorted(grouped.items()):
        report=validate_cohort(list(rounds.values()))
        cohorts.append({"checkpoint":checkpoint,"version":version,**report})
    return {"status":"research_only","model":"ridge_delta_bps_shadow_lab_v1",
            "min_rounds_per_version_checkpoint":MIN_COMPLETE_ROUNDS,
            "cohorts":cohorts,"auto_promote":False,
            "settlement_source":"Only observed published final-minute official BRTI means",
            "note":"Never train on active rounds, inferred BRTI points, repeated checkpoints combined as independent observations, or validation rows. No live trading or prediction logic is changed by this laboratory."}
