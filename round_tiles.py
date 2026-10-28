"""Compact, bounded history of independently verified 5m and 15m outcomes.

The 15m official winner is derived from the archived WIN/LOSS for a genuine
FINAL_15M AI_REVIEW call (its settlement checker verified the outcome).
The 5m winner must be the saved official settlement winner. No backfills,
quotes, odds, market value or paper trades are used.
"""
from datetime import datetime, timedelta, timezone

TILE_LIMIT = 36
MAX_AI_ROWS = 144
LATE_MIN_LEFT = 120
LATE_MAX_LEFT = 150


def _side(value):
    return value if value in ("UP", "DOWN") else None


def _correct(direction, winner, result):
    if _side(direction) is None or _side(winner) is None or result not in ("WIN", "LOSS"):
        return None
    actual = direction == winner
    return actual if (result == "WIN") == actual else None


def _late_valid(market, created):
    """Only prospective 120–150-second-left checkpoints count."""
    try:
        stamp = market.split("btc-updown-5m-", 1)[1]
        start = datetime.strptime(stamp, "%Y-%m-%d-%H%Mz").replace(tzinfo=timezone.utc)
        left = (start + timedelta(minutes=5)).timestamp() - float(created)
        return LATE_MIN_LEFT <= left <= LATE_MAX_LEFT
    except (IndexError, TypeError, ValueError, OverflowError):
        return False


def _summary(rows, correct_key):
    win = sum(x.get("winner") == "UP" for x in rows)
    down = sum(x.get("winner") == "DOWN" for x in rows)
    scored = sum(x.get(correct_key) is not None for x in rows)
    right = sum(x.get(correct_key) is True for x in rows)
    return {"rounds": len(rows), "up": win, "down": down, "scored": scored,
            "correct": right, "incorrect": scored-right,
            "accuracy_pct": round(100 * right / scored, 1) if scored else None}


def fifteen_rounds(execute):
    """Last 36 unique, settled, genuine 15m AI rounds (not indicator fallbacks)."""
    rows = execute("""SELECT market,direction,result,created_at FROM ai_decisions
        WHERE phase='FINAL_15M' AND source='AI_REVIEW'
          AND result IN ('WIN','LOSS') AND direction IN ('UP','DOWN')
        ORDER BY created_at DESC,id DESC LIMIT 144""", fetch=True)
    recent = []
    seen = set()
    for market, predicted, result, captured in rows:
        if not isinstance(market, str) or "btc-updown-15m-" not in market or market in seen:
            continue
        # Only genuine officially graded WIN/LOSS rows are eligible.
        winner = predicted if result == "WIN" else ("DOWN" if predicted == "UP" else "UP")
        seen.add(market)
        recent.append({"market": market, "winner": winner, "prediction": predicted,
                       "correct": result == "WIN", "captured_at": captured})
        if len(recent) == TILE_LIMIT:
            break
    recent.reverse()  # Oldest on the left, newest on the right.
    return {"basis": "Officially graded 15m FINAL_15M AI_REVIEW calls; winner inferred from archived WIN/LOSS",
            "summary": _summary(recent, "correct"), "rounds": recent}


def five_rounds(execute):
    """Use one small indexed join; no historical scans or fabricated late calls."""
    rows = execute("""SELECT e.market,e.direction,e.result,e.winner,
            l.direction,l.result,l.winner,l.created_epoch
        FROM five_min_predictions e
        LEFT JOIN five_min_late_predictions l ON e.market=l.market
        WHERE e.result IN ('WIN','LOSS') AND e.winner IN ('UP','DOWN')
        ORDER BY e.created_epoch DESC,e.market DESC LIMIT 36""", fetch=True)
    recent = []
    for market, early_dir, early_result, winner, late_dir, late_result, late_winner, late_epoch in rows:
        early_correct = _correct(early_dir, winner, early_result)
        if early_correct is None or not isinstance(market, str):
            continue
        late_correct = None
        if (late_winner == winner and _late_valid(market, late_epoch)):
            late_correct = _correct(late_dir, winner, late_result)
        recent.append({"market": market, "winner": winner,
                       "early_prediction": early_dir, "early_correct": early_correct,
                       "late_prediction": late_dir if late_correct is not None else None,
                       "late_correct": late_correct})
    recent.reverse()
    late_summary = _summary(recent, "late_correct")
    return {"basis": "Verified 5m early settlements; late only if separately graded and captured 120–150s left",
            "summary": _summary(recent, "early_correct"),
            "late_summary": {"scored": late_summary["scored"],
                             "correct": late_summary["correct"],
                             "incorrect": late_summary["incorrect"],
                             "accuracy_pct": late_summary["accuracy_pct"]},
            "rounds": recent}


def report(execute):
    return {"status": "observed", "limit": TILE_LIMIT,
            "ordering": "oldest_to_newest",
            "fifteen": fifteen_rounds(execute),
            "five": five_rounds(execute),
            "note": "Tiles show only saved, officially graded rounds with real predictions. Missing and pending rounds are not invented. An outlined tile means the model was correct; faded means incorrect. The 5m late row may have gaps for missing or unverified checkpoints."}
