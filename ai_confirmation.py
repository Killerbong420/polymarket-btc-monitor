"""Server-side OpenAI confirmation. Never expose API credentials to browser clients."""
import json
import os
import time
import httpx

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MIN_INTERVAL = max(60, int(os.getenv("AI_INTERVAL_SECONDS", "120")))
_last_request = 0.0
_cache = {"status": "not_configured", "verdict": "WAIT", "reason": "Add OPENAI_API_KEY in Render environment settings.", "model": MODEL}
_lock = None

_auth_cache = {"status": "pending", "reason": "Authentication check has not run."}
_auth_last = 0.0

def auth_state():
    if not os.getenv("OPENAI_API_KEY"):
        return {"status": "not_configured", "reason": "Set OPENAI_API_KEY in Render."}
    return dict(_auth_cache)

async def check_auth(client):
    """Check model access without inference or exposing secrets."""
    global _auth_last, _auth_cache
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return auth_state()
    if time.monotonic() - _auth_last < 1800:
        return auth_state()
    _auth_last = time.monotonic()
    try:
        r = await client.get("https://api.openai.com/v1/models/" + MODEL,
            headers={"Authorization": "Bearer " + key}, timeout=12)
        if r.status_code == 200:
            if _auth_cache.get("status") != "inference_verified":
                _auth_cache = {"status": "authenticated", "reason": "Key accepted; inference and billing not yet verified."}
        else:
            category = {401:"invalid_api_key",403:"permission_denied",404:"model_not_found",429:"rate_limit_or_quota"}.get(r.status_code,"http_error")
            _auth_cache = {"status": "error", "http_status": r.status_code, "reason": category}
    except Exception as exc:
        _auth_cache = {"status": "error", "reason": "Connection failed: " + type(exc).__name__}
    return auth_state()


def public_state():
    if not os.getenv("OPENAI_API_KEY"):
        return {"status": "not_configured", "verdict": "WAIT", "reason": "Set OPENAI_API_KEY securely in Render to activate AI confirmation.", "model": MODEL}
    return dict(_cache)

async def confirm(client: httpx.AsyncClient, market, phase, signal, flip, score, external, price_to_beat=None, seconds_left=None):
    global _last_request, _cache, _auth_cache
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        _cache = public_state()
        return _cache
    if not market or "btc-updown-15m-" not in str(market) or not external or external.get("price") is None:
        _cache = {"status": "waiting_for_data", "verdict": "WAIT", "reason": "Awaiting a verified 15-minute market and fresh BTC candles.", "model": MODEL}
        return _cache
    if signal not in ("UP", "DOWN"):
        fast = float(external.get("momentum_pct") or 0)
        slow = float(external.get("long_momentum_pct") or 0)
        signal = "UP" if fast * 2 + slow >= 0 else "DOWN"
    now = time.monotonic()
    if now - _last_request < MIN_INTERVAL:
        if _cache.get("market") != market:
            _cache = {"status": "waiting_for_refresh", "verdict": "WAIT", "market": market, "model": MODEL, "reason": "New 15-minute round; awaiting next AI refresh."}
        return _cache
    _last_request = now
    snapshot = {
        "market": str(market), "duration_minutes": 15, "seconds_left": seconds_left,
        "official_price_to_beat": price_to_beat, "settlement_index": "BRTI; spot price is only a proxy", "phase": str(phase), "indicator_signal": signal,
        "possible_flip": flip, "indicator_score": score,
        "external_price": external.get("price"),
        "momentum_pct": external.get("momentum_pct"),
        "long_momentum_pct": external.get("long_momentum_pct"),
        "volume_ratio": external.get("volume_ratio"),
        "pressure": external.get("pressure") if external.get("pressure_available") else None,
        "data_source": external.get("source"),
        "candle_age_seconds": external.get("candle_age_seconds"),
        "chart_indicators": external.get("chart"),
        "chart_pattern": external.get("chart_pattern"),
        "signal_quality": external.get("signal_quality"),
        "graph_evidence": {**(external.get("graph_evidence") or {}),
            "candles_ohlcv": (external.get("graph_evidence") or {}).get("candles_ohlcv", [])[-20:]},
    }
    schema = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["UP", "DOWN"]},
        "reason": {"type": "string"},
        "flip_risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "UNKNOWN"]},
        "confidence": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]}
    }, "required": ["verdict", "reason", "flip_risk", "confidence"], "additionalProperties": False}
    payload = {
        "model": MODEL, "store": False,
        "instructions": (
            "Act as a BTC price-action chart analyst. Read the chronological 1-minute OHLCV candles "
            "in graph_evidence (oldest to newest), not just the precomputed indicator signal. "
            "Assess higher highs/lows, candle bodies and wicks, breakout or rejection near support/resistance, "
            "momentum acceleration/fading, EMA alignment, RSI and volume where provided. "
            "Weigh recent structure most heavily; compare contradictory trade pressure or indicators. "
            "Predict the current 15-minute contract settlement direction relative to its official price to beat, not the next candle. Account for seconds_left. Spot prices are proxies, not BRTI settlement. Select UP or DOWN, never WAIT "
            "when usable candles exist; confidence LOW when unclear or stale. "
            "Explain the specific chart evidence for the direction, not just generic bullish/bearish claims. "
            "Never invent unavailable data, probabilities or certainty; research only, not trading advice. "
            "Keep reason under 180 characters."
        ),
        "input": json.dumps(snapshot, separators=(",", ":")),
        "text": {"format": {"type": "json_schema", "name": "btc_confirmation",
            "strict": True, "schema": schema}},
        "max_output_tokens": 240,
    }
    try:
        r = await client.post("https://api.openai.com/v1/responses",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json=payload, timeout=22)
        r.raise_for_status()
        body = r.json()
        text_parts = [part.get("text", "") for item in body.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", []) if part.get("type") == "output_text"]
        if not text_parts:
            raise ValueError("Model returned no usable text")
        parsed = json.loads("".join(text_parts))
        if parsed.get("verdict") not in ("UP", "DOWN"):
            raise ValueError("Invalid model verdict")
        _auth_cache = {"status": "inference_verified", "reason": "OpenAI inference succeeded."}
        _cache = {"status": "ready", "verdict": parsed["verdict"],
            "reason": str(parsed.get("reason", ""))[:240],
            "flip_risk": parsed.get("flip_risk", "UNKNOWN"),
            "confidence": parsed.get("confidence", "LOW"),
            "signal": signal, "market": market, "phase": phase,
            "model": MODEL, "updated_epoch": time.time()}
    except httpx.HTTPStatusError as exc:
        # Expose only a numeric HTTP status and predefined category.
        status = exc.response.status_code
        categories = {400: "request_invalid", 401: "invalid_api_key",
            403: "permission_denied", 404: "model_or_endpoint_unavailable",
            408: "request_timeout", 429: "rate_limit_or_quota",
            500: "provider_error", 502: "provider_error", 503: "provider_unavailable"}
        category = categories.get(status, "http_error")
        _cache = {"status": "error", "verdict": "WAIT",
            "reason": "OpenAI HTTP " + str(status) + " (" + category + "). Check API billing, key permissions, and model access.",
            "http_status": status, "error_category": category, "model": MODEL}
    except Exception as exc:
        # Never put secrets, full URLs, or response bodies in public diagnostics.
        _cache = {"status": "error", "verdict": "WAIT",
            "reason": "OpenAI request failed (" + type(exc).__name__ + "). Check API key, model access and billing in Render.",
            "model": MODEL}
    return _cache

async def halfway_decision(client, market, duration, indicator_direction, score, external, price_to_beat=None):
    """One bounded AI review at the halfway FINAL checkpoint; never invent missing observations."""
    fallback={"direction":indicator_direction,"source":"INDICATORS_FALLBACK","reason":"AI unavailable; indicator decision retained"}
    if duration != 15 or "btc-updown-15m-" not in str(market):
        return {**fallback,"reason":"AI restricted to verified 15-minute markets"}
    key=os.getenv("OPENAI_API_KEY")
    if not key: return {**fallback,"reason":"OPENAI_API_KEY not configured","evidence_available":False}
    try:
        target=float(price_to_beat) if price_to_beat is not None else None
        if target is not None and target<=0: target=None
    except (TypeError,ValueError): target=None
    spot=external.get("price")
    try:
        spot_delta_pct=round((float(spot)/target-1)*100,5) if target and spot is not None else None
    except (ValueError,TypeError,ZeroDivisionError): spot_delta_pct=None
    chart=external.get("chart") or {}
    pattern=external.get("chart_pattern") or {}
    quality=external.get("signal_quality") or {}
    missing=[]
    if target is None: missing.append("official_price_to_beat")
    if spot is None: missing.append("spot_price")
    if not chart: missing.append("technical_chart_indicators")
    if not external.get("graph_evidence"): missing.append("completed_price_graph_candles")
    if not pattern: missing.append("chart_pattern")
    if not external.get("pressure_available"): missing.append("verified_buy_sell_pressure")
    if external.get("volume_ratio") is None: missing.append("volume_ratio")
    if external.get("candle_age_seconds") is None: missing.append("candle_freshness")
    snapshot={
        "market":market,"duration_minutes":duration,
        "indicator_direction":indicator_direction,"indicator_score":score,
        "official_polymarket_price_to_beat":target,
        "official_settlement_index":"BRTI (not Coinbase/Kraken spot)",
        "spot_price":spot,"spot_source":external.get("source"),
        "spot_vs_official_target_pct_proxy_only":spot_delta_pct,
        "fast_momentum_pct":external.get("momentum_pct"),
        "long_momentum_pct":external.get("long_momentum_pct"),
        "volume_ratio":external.get("volume_ratio"),
        "buy_sell_pressure":external.get("pressure") if external.get("pressure_available") else None,
        "chart":{"ema9":chart.get("ema9"),"ema21":chart.get("ema21"),
                 "ema_trend":chart.get("ema_trend"),"rsi14":chart.get("rsi14"),
                 "atr14_pct":chart.get("atr14_pct")},
        "pattern":{"type":pattern.get("pattern"),"bias":pattern.get("bias"),
                   "current_behavior":pattern.get("current_behavior"),
                   "structure":pattern.get("structure"),
                   "momentum_3m_pct":pattern.get("momentum_3m_pct"),
                   "momentum_5m_pct":pattern.get("momentum_5m_pct"),
                   "momentum_10m_pct":pattern.get("momentum_10m_pct"),
                   "prior_5m_pct":pattern.get("prior_5m_pct"),
                   "momentum_acceleration_5m_pct":pattern.get("momentum_acceleration_5m_pct"),
                   "range_position_pct":pattern.get("range_position_pct"),
                   "distance_to_support_pct":pattern.get("distance_to_support_pct"),
                   "distance_to_resistance_pct":pattern.get("distance_to_resistance_pct"),
                   "last_candle_body_direction":pattern.get("last_candle_body_direction"),
                   "last_candle_upper_wick_ratio":pattern.get("last_candle_upper_wick_ratio"),
                   "last_candle_lower_wick_ratio":pattern.get("last_candle_lower_wick_ratio"),
                   "support":pattern.get("support"),"resistance":pattern.get("resistance"),
                   "range_pct":pattern.get("range_pct"),
                   "volume_spike":pattern.get("volume_spike"),
                   "note":pattern.get("note")},
        "graph_evidence":external.get("graph_evidence"),
        "signal_quality":quality,
        "candle_age_seconds":external.get("candle_age_seconds"),
        "missing_evidence":missing}
    schema={"type":"object","properties":{"direction":{"type":"string","enum":["UP","DOWN"]},
        "reason":{"type":"string"},"confidence":{"type":"string","enum":["LOW","MEDIUM","HIGH"]}},
        "required":["direction","reason","confidence"],"additionalProperties":False}
    payload={"model":MODEL,"store":False,"instructions":(
        "At the halfway checkpoint, review the provided BTC market observations and make exactly one binary research forecast: UP or DOWN. Never output WAIT.  "
        "You may disagree with the indicator direction when concrete evidence supports it. "
        "Use only supplied data; do not invent BRTI values, order flow, odds or probabilities. "
        "When evidence is weak or conflicting, still choose the more supported direction and set confidence LOW; explicitly explain the uncertainty. This is a research forecast, not a trade instruction. "
        "Analyze the chronological OHLCV candle graph itself: swing highs/lows, trend structure, recent candle bodies/wicks, support/resistance, breakout/rejection, and reversal versus continuation. Explicitly weigh current_behavior, acceleration versus fading, pullback versus reversal, and price location inside its range; do not treat UP/DOWN bias as sufficient evidence. "
        "Consider every supplied evidence field, contradictions, proximity to the official target, "
        "freshness and missing evidence. Spot vs BRTI target is a proxy, not the settlement index. "
        "Do not claim unavailable buy/sell pressure or other missing data. "
        "Choose UP or DOWN even when uncertain, marking confidence LOW and explaining the most important factors. "
        "Keep the reason under 160 characters."),
        "input":json.dumps(snapshot,default=str,separators=(",",":")),
        "text":{"format":{"type":"json_schema","name":"btc_halfway_final","strict":True,"schema":schema}},
        "max_output_tokens":200}
    try:
        res=await client.post("https://api.openai.com/v1/responses",
            headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
            json=payload,timeout=12)
        res.raise_for_status()
        body=res.json()
        chunks=[p.get("text","") for x in body.get("output",[]) if x.get("type")=="message"
                for p in x.get("content",[]) if p.get("type")=="output_text"]
        decision=json.loads("".join(chunks))
        if decision.get("direction") not in ("UP","DOWN"): raise ValueError("Invalid direction")
        return {"direction":decision["direction"],"source":"AI_REVIEW",
            "reason":str(decision.get("reason",""))[:160],
            "ai_direction":decision["direction"],"ai_confidence":decision.get("confidence","LOW"),
            "evidence":snapshot}
    except Exception as exc:
        return {**fallback,"reason":"AI request unavailable ("+type(exc).__name__+")",
                "evidence":snapshot}

