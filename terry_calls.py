"""Read-only TerryCalls 15m comparison: no orders, no official-target substitution.

Provider requests run in one bounded background worker, never in /api/live.
The provider's chances are its estimates, not calibrated odds from our model.
"""
import asyncio
from datetime import datetime, timezone
import os
import time

import httpx

LIVE_URL = "https://terrycalls.com/api/v1/calls"
HISTORY_URL = "https://terrycalls.com/api/v1/calls/history"
_CACHE = {"payload": None, "received": 0.0, "error": None}
_SUMMARY = {"status": "collecting", "paired": 0, "settled": 0}
_LAST_SAVED_MARKET = None
_TABLE_READY = False


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _pct(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return None
    return round(value * 100, 1)


def _side(value):
    side = str(value or "").upper()
    return side if side in ("UP", "DOWN") else None


def _market(payload):
    if not isinstance(payload, dict) or payload.get("stale") is True:
        return None
    markets = payload.get("markets")
    if not isinstance(markets, list):
        return None
    for entry in markets:
        if isinstance(entry, dict) and entry.get("horizon") == "15m" and "btc-updown-15m-" in str(entry.get("market") or ""):
            return entry
    return None


def snapshot(local, now=None):
    """Make a same-market, freshness-gated public snapshot; never expose the key."""
    now = time.time() if now is None else now
    if not os.getenv("TERRY_API_KEY"):
        return {"status": "not_configured", "source": "TerryCalls", "comparison_history": dict(_SUMMARY)}
    payload, received = _CACHE["payload"], _CACHE["received"]
    base = {"source": "TerryCalls", "comparison_history": dict(_SUMMARY)}
    if not isinstance(payload, dict) or now - received > 35 or received > now + 2:
        return {**base, "status": "unavailable", "reason": _CACHE["error"] or "refresh_pending"}
    entry = _market(payload)
    if entry is None or _epoch(payload.get("updated_at")) < now - 35 or _epoch(payload.get("updated_at")) > now + 10:
        return {**base, "status": "stale", "reason": "provider_data_stale"}
    slug = entry.get("market")
    if slug != local.get("market") or _epoch(entry.get("window_end")) <= now:
        return {**base, "status": "market_mismatch", "reason": "waiting_for_matching_current_15m_market"}
    first = entry.get("call") if isinstance(entry.get("call"), dict) else {}
    chances = entry.get("chance_now") if isinstance(entry.get("chance_now"), dict) else {}
    direction = _side(first.get("side"))
    status = "live" if direction else "waiting"
    guarded = local.get("guarded_primary") or {}
    fresh_ours = (guarded.get("status") == "available"
                  and _side(guarded.get("direction")) is not None
                  and local.get("status") == "running"
                  and 0 <= now - _epoch(local.get("updated")) <= 20)
    ours = _side(guarded.get("direction")) if fresh_ours else None
    return {
        **base, "status": status, "market": slug,
        "provider_updated_at": payload.get("updated_at"),
        "terry_call": {"side": direction, "chance_pct": _pct(first.get("chance")),
                       "at": first.get("at"), "entry_price": first.get("price")} if direction else None,
        "terry_chance_now": {"up_pct": _pct(chances.get("up")), "down_pct": _pct(chances.get("down"))},
        "our_call": {"side": ours, "source": "guarded_live_15m"} if ours else None,
        "comparison": ("AGREE" if direction == ours else "DISAGREE") if direction and ours else "UNAVAILABLE",
        "note": "Independent live calls, sampled together after Terry's original call; not identical call timestamps.",
    }


def _setup_table(execute):
    execute("""CREATE TABLE IF NOT EXISTS terry_15m_comparisons (
        market TEXT PRIMARY KEY, captured_at TEXT NOT NULL, ours TEXT NOT NULL,
        terry TEXT NOT NULL, terry_call_at TEXT, result TEXT NOT NULL DEFAULT 'PENDING')""")


def _save_pair(execute, sample):
    execute("""INSERT INTO terry_15m_comparisons(market,captured_at,ours,terry,terry_call_at)
        VALUES(?,?,?,?,?) ON CONFLICT(market) DO NOTHING""", (
        sample["market"], datetime.now(timezone.utc).isoformat(),
        sample["our_call"]["side"], sample["terry_call"]["side"],
        sample["terry_call"].get("at")))


def _grade_and_summarize(execute, windows):
    """Grade only paired, recorded live observations against provider-reported settlement."""
    rows = execute("""SELECT market,ours,terry,result FROM terry_15m_comparisons
                      ORDER BY captured_at DESC LIMIT 500""", fetch=True)
    history = {w.get("market"): w for w in windows if isinstance(w, dict) and w.get("horizon") == "15m"}
    for market, ours, terry, old_result in rows:
        if old_result in ("UP", "DOWN"):
            continue
        found = history.get(market)
        if not found or _side(found.get("result")) is None:
            continue
        past_call = found.get("call") if isinstance(found.get("call"), dict) else {}
        if _side(past_call.get("side")) != terry:
            continue
        execute("""UPDATE terry_15m_comparisons SET result=?
                   WHERE market=? AND result='PENDING'""", (_side(found["result"]), market))
    rows = execute("""SELECT market,ours,terry,result FROM terry_15m_comparisons
                      ORDER BY captured_at DESC LIMIT 500""", fetch=True)
    settled = [r for r in rows if r[3] in ("UP", "DOWN")]
    count = len(settled)
    our_right = sum(r[1] == r[3] for r in settled)
    terry_right = sum(r[2] == r[3] for r in settled)
    return {
        "status": "observed", "paired": len(rows), "settled": count,
        "agree": sum(r[1] == r[2] for r in rows),
        "ours_right": our_right, "terry_right": terry_right,
        "ours_accuracy_pct": round(100 * our_right / count, 1) if count else None,
        "terry_accuracy_pct": round(100 * terry_right / count, 1) if count else None,
        "basis": "Paired live samples; provider-reported Polymarket results, not independent settlement audit. Last 500 pairs.",
    }


async def worker(local_state, execute):
    """Sole owner of provider API polling and comparison writes."""
    global _LAST_SAVED_MARKET, _TABLE_READY, _SUMMARY
    last_history = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0), follow_redirects=False) as client:
        while True:
            delay = 10
            key = os.getenv("TERRY_API_KEY")
            if not key:
                _CACHE.update(payload=None, received=0.0, error="not_configured")
                await asyncio.sleep(60)
                continue
            headers = {"X-API-Key": key}
            try:
                response = await client.get(LIVE_URL, params={"horizon": "15m"}, headers=headers)
                if response.status_code == 429:
                    try:
                        delay = max(10, min(300, int(response.headers.get("Retry-After", "60"))))
                    except ValueError:
                        delay = 60
                    _CACHE["error"] = "rate_limited"
                else:
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict):
                        raise ValueError("invalid_provider_response")
                    _CACHE.update(payload=data, received=time.time(), error=None)
                    if not _TABLE_READY:
                        await asyncio.to_thread(_setup_table, execute)
                        _TABLE_READY = True
                    sample = snapshot(local_state())
                    if (sample.get("status") == "live" and sample.get("our_call")
                            and sample.get("market") != _LAST_SAVED_MARKET):
                        await asyncio.to_thread(_save_pair, execute, sample)
                        _LAST_SAVED_MARKET = sample["market"]
                    if time.monotonic() - last_history >= 180:
                        try:
                            historical = await client.get(HISTORY_URL, params={"horizon": "15m", "days": 7, "limit": 500}, headers=headers)
                            historical.raise_for_status()
                            windows = historical.json().get("windows", [])
                            if not isinstance(windows, list):
                                raise ValueError("invalid_provider_history")
                            _SUMMARY = await asyncio.to_thread(_grade_and_summarize, execute, windows)
                            last_history = time.monotonic()
                        except (httpx.HTTPError, ValueError, TypeError, KeyError):
                            # A history outage never slows or changes current predictions.
                            last_history = time.monotonic() - 120
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                _CACHE["error"] = type(exc).__name__
            except Exception:
                # Including database failures: keep the existing bot responsive.
                _CACHE["error"] = "comparison_worker_error"
            await asyncio.sleep(delay)
