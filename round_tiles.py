"""Compact history of independently verified 15m settlement outcomes.

Official winners are derived from archived WIN/LOSS for RULE_BASED
FINAL_15M signals. Historical AI calls remain archived separately.
"""
TILE_LIMIT = 36
MAX_ROWS = 144


def _side(value):
    return value if value in ("UP", "DOWN") else None


def _summary(rows, correct_key):
    win = sum(x.get("winner") == "UP" for x in rows)
    down = sum(x.get("winner") == "DOWN" for x in rows)
    scored = sum(x.get(correct_key) is not None for x in rows)
    right = sum(x.get(correct_key) is True for x in rows)
    return {"rounds": len(rows), "up": win, "down": down, "scored": scored,
            "correct": right, "incorrect": scored-right,
            "accuracy_pct": round(100 * right / scored, 1) if scored else None}


def fifteen_rounds(execute):
    """Last 36 unique, officially settled rule-based 15-minute rounds."""
    rows = execute("""SELECT market,direction,result,created_at FROM signals
        WHERE phase='FINAL_15M' AND decision_source='RULE_BASED'
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
    return {"basis": "Officially graded RULE_BASED FINAL_15M signals; winner inferred from archived WIN/LOSS",
            "summary": _summary(recent, "correct"), "rounds": recent}


def report(execute):
    return {"status": "observed", "limit": TILE_LIMIT,
            "ordering": "oldest_to_newest",
            "fifteen": fifteen_rounds(execute),
            "note": "Shows only saved, officially graded 15-minute rounds with real calls. Pending outcomes are not invented; outlines show correct predictions."}
