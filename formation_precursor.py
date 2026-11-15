"""Early Coinbase formation precursors and forward-only 5/10/15-minute audit.

A precursor is a strong observed impulse BEFORE an eligible chart flag forms.
It is not a flag, a future breakout, a calibrated probability or a Polymarket
settlement/trading signal. No retroactive predictions or synthetic candles.
"""
import hashlib
import math

SOURCE = "Coinbase Exchange BTC-USD"
HORIZONS = (5, 10, 15)


def detect_precursor(bars, atr):
    """Return a conservative pre-formation observation on completed 1m bars."""
    if len(bars) < 35 or not math.isfinite(atr) or atr <= 0:
        return None
    closes = [b[4] for b in bars]
    for pullback in (1, 0):
        end = len(bars) - 1 - pullback
        for length in (3, 4, 5, 6):
            start = end - length
            if start < 20:
                continue
            move = closes[end] - closes[start]
            direction = 1 if move > 0 else -1
            size = abs(move)
            if size < max(2.35 * atr, closes[-1] * .00055):
                continue
            steps = [direction * (closes[i] - closes[i - 1])
                     for i in range(start + 1, end + 1)]
            if sum(x > 0 for x in steps) < length - 1:
                continue
            path = sum(abs(x) for x in steps)
            if path <= 0 or size / path < .80:
                continue
            volumes = [b[5] for b in bars[start + 1:end + 1]]
            baseline = [b[5] for b in bars[start - 15:start]]
            baseline_mean = sum(baseline) / len(baseline)
            if baseline_mean <= 0:
                continue
            volume_ratio = sum(volumes) / len(volumes) / baseline_mean
            # Quiet sharp candles alone make poor advance warnings.
            if volume_ratio < .90:
                continue
            retreat = 0.0
            if pullback:
                retreat = direction * (closes[end] - closes[-1]) / size
                if not .06 <= retreat <= .42:
                    continue
                # One genuine countertrend close, not a large reversal.
                if direction * (closes[-1] - closes[end]) >= 0:
                    continue
            else:
                if steps[-1] <= 0:
                    continue
            segment = bars[start + 1:end + 1]
            trigger = max(b[2] for b in segment) if direction > 0 else min(b[1] for b in segment)
            invalid = closes[start] + direction * .25 * size
            if direction * (trigger - invalid) <= 0:
                continue
            return {
                "status": "precursor", "phase": "FIRST_PULLBACK" if pullback else "IMPULSE",
                "hint": "BULL_FLAG" if direction > 0 else "BEAR_FLAG",
                "bias": "UP" if direction > 0 else "DOWN",
                "anchor_epoch": int(bars[end][0]),
                "last_completed_epoch": int(bars[-1][0]) + 60,
                "reference_usd": round(closes[-1], 2),
                "trigger_usd": round(trigger, 2),
                "invalidation_usd": round(invalid, 2),
                "impulse_usd": round(size, 2),
                "impulse_minutes": length,
                "volume_ratio": round(volume_ratio, 2),
                "research_horizons_minutes": list(HORIZONS),
                "probability_calibrated": False,
                "breakout_confirmed": False,
                "note": "Unconfirmed early impulse; not an established flag or forecast win probability",
            }
    return None


def setup_precursor_tables(execute):
    execute("""CREATE TABLE IF NOT EXISTS formation_precursors (
      id TEXT PRIMARY KEY, market TEXT NOT NULL, source TEXT NOT NULL,
      hint TEXT NOT NULL, direction INTEGER NOT NULL, stage TEXT NOT NULL,
      first_seen_epoch INTEGER NOT NULL, last_bar_epoch INTEGER NOT NULL,
      reference_usd REAL NOT NULL, trigger_usd REAL NOT NULL,
      invalidation_usd REAL NOT NULL, status TEXT NOT NULL,
      return_5m_pct REAL, return_10m_pct REAL, return_15m_pct REAL,
      invalidated_epoch INTEGER)""")
    execute("CREATE INDEX IF NOT EXISTS precursor_open ON formation_precursors (source,status,last_bar_epoch)")
    execute("CREATE INDEX IF NOT EXISTS precursor_recent ON formation_precursors (source,first_seen_epoch DESC)")


def _number(value):
    try:
        if isinstance(value, bool):
            return None
        val = float(value)
        return val if math.isfinite(val) else None
    except (ValueError, TypeError, OverflowError):
        return None


def record_precursor_bar(execute, market, signal, candle, bar_epoch, *, recovery=False):
    """Advance using this actual completed Coinbase minute, then observe a new precursor."""
    close = _number(candle[4])
    if close is None or close <= 0:
        return
    pending = execute("""SELECT id,last_bar_epoch,first_seen_epoch,reference_usd,
       direction,invalidation_usd,return_5m_pct,return_10m_pct,return_15m_pct,
       invalidated_epoch FROM formation_precursors WHERE source=? AND status='OPEN'
       ORDER BY first_seen_epoch DESC LIMIT 80""", (SOURCE,), fetch=True)
    for row in pending:
        (ident, last_epoch, first_epoch, ref, direction, invalid,
         r5, r10, r15, invalidated) = row
        if bar_epoch <= last_epoch:
            continue
        if bar_epoch != last_epoch + 60:
            execute("UPDATE formation_precursors SET status='GAP',last_bar_epoch=? WHERE id=? AND status='OPEN'",
                    (bar_epoch, ident))
            continue
        elapsed = bar_epoch - first_epoch
        if elapsed > 900:
            execute("UPDATE formation_precursors SET status='GAP',last_bar_epoch=? WHERE id=? AND status='OPEN'",
                    (bar_epoch, ident))
            continue
        # Score the same first-warning reference at fixed forward close horizons,
        # INCLUDING calls subsequently invalidated. Never select only winners.
        signed_move = 100 * direction * (close / ref - 1)
        if elapsed == 300:
            r5 = round(signed_move, 5)
        elif elapsed == 600:
            r10 = round(signed_move, 5)
        elif elapsed == 900:
            r15 = round(signed_move, 5)
        if invalidated is None and direction * (close - invalid) <= 0:
            invalidated = bar_epoch
        status = "FINISHED" if elapsed == 900 else "OPEN"
        execute("""UPDATE formation_precursors SET status=?,last_bar_epoch=?,
           return_5m_pct=?,return_10m_pct=?,return_15m_pct=?,invalidated_epoch=?
           WHERE id=? AND status='OPEN' AND last_bar_epoch=?""",
           (status, bar_epoch, r5, r10, r15, invalidated, ident, last_epoch))
    if recovery or not isinstance(signal, dict) or signal.get("source") != SOURCE:
        return
    precursor = signal.get("precursor")
    if not isinstance(precursor, dict) or precursor.get("status") != "precursor":
        return
    side = 1 if precursor.get("bias") == "UP" else -1 if precursor.get("bias") == "DOWN" else 0
    hint = precursor.get("hint")
    observed = _number(precursor.get("reference_usd"))
    trigger = _number(precursor.get("trigger_usd"))
    invalid = _number(precursor.get("invalidation_usd"))
    anchor = _number(precursor.get("anchor_epoch"))
    if (side == 0 or hint != ("BULL_FLAG" if side == 1 else "BEAR_FLAG")
        or observed is None or abs(observed-close) > max(.011, close * .0000001)
        or trigger is None or invalid is None or invalid <= 0
        or side * (trigger - invalid) <= 0
        or anchor is None or anchor != int(anchor)
        or not 0 <= bar_epoch - anchor <= 8 * 60
        or precursor.get("last_completed_epoch") != bar_epoch
        or signal.get("status") != "monitoring"):
        return
    # Avoid creating overlapping same-side observations every minute.
    duplicate = execute("""SELECT id FROM formation_precursors
       WHERE source=? AND direction=? AND first_seen_epoch>? ORDER BY
       first_seen_epoch DESC LIMIT 1""",
       (SOURCE, side, bar_epoch - 10 * 60), fetch=True)
    if duplicate:
        return
    ident = hashlib.sha256((SOURCE + "|" + str(side) + "|" + str(bar_epoch)).encode()).hexdigest()[:32]
    execute("""INSERT INTO formation_precursors (
       id,market,source,hint,direction,stage,first_seen_epoch,last_bar_epoch,
       reference_usd,trigger_usd,invalidation_usd,status)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,'OPEN') ON CONFLICT(id) DO NOTHING""",
       (ident,market,SOURCE,hint,side,precursor["phase"],bar_epoch,bar_epoch,
        observed,trigger,invalid))


def precursor_stats(execute):
    """Forward-only directional accuracy; unobserved horizons are not losses."""
    rows = execute("""SELECT hint,stage,first_seen_epoch,status,return_5m_pct,
        return_10m_pct,return_15m_pct,invalidated_epoch
        FROM formation_precursors WHERE source=? ORDER BY
        first_seen_epoch DESC LIMIT 200""", (SOURCE,), fetch=True)
    horizons = []
    for idx, minutes in enumerate(HORIZONS):
        samples = [float(row[4 + idx]) for row in rows if row[4 + idx] is not None]
        n = len(samples)
        horizons.append({
            "minutes": minutes, "samples": n,
            "direction_correct": sum(x > 0 for x in samples),
            "direction_incorrect": sum(x < 0 for x in samples),
            "flat": sum(x == 0 for x in samples),
            "direction_accuracy_pct": round(100 * sum(x > 0 for x in samples) / n, 1) if n >= 20 else None,
            "avg_signed_return_pct": round(sum(samples) / n, 5) if n >= 20 else None,
            "sample_sufficient": n >= 20,
        })
    return {
        "status": "observed" if rows else "collecting",
        "source": SOURCE, "cohort_limit": 200, "horizons": horizons,
        "recent": [{
            "hint": row[0], "stage": row[1], "first_seen_epoch": row[2],
            "status": row[3], "return_5m_pct": row[4],
            "return_10m_pct": row[5], "return_15m_pct": row[6],
            "invalidated_epoch": row[7],
        } for row in rows[:6]],
        "definition": "Unconfirmed Coinbase impulse observations. Signed Coinbase close-to-close returns exactly 5/10/15 completed minutes after first warning; all observed outcomes included even after invalidation. Missing candles unscored. Not breakout odds or Polymarket settlement.",
    }
