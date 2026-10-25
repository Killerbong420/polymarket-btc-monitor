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

def public_state():
    if not os.getenv("OPENAI_API_KEY"):
        return {"status": "not_configured", "verdict": "WAIT", "reason": "Set OPENAI_API_KEY securely in Render to activate AI confirmation.", "model": MODEL}
    return dict(_cache)

async def confirm(client: httpx.AsyncClient, market, phase, signal, flip, score, external):
    global _last_request, _cache
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        _cache = public_state()
        return _cache
    if signal not in ("UP", "DOWN") or not market or not external:
        _cache = {"status": "waiting_for_signal", "verdict": "WAIT", "reason": "No verified live market and directional indicator signal.", "model": MODEL}
        return _cache
    now = time.monotonic()
    if now - _last_request < MIN_INTERVAL:
        return _cache
    _last_request = now
    snapshot = {
        "market": str(market), "phase": str(phase), "indicator_signal": signal,
        "possible_flip": flip, "indicator_score": score,
        "external_price": external.get("price"),
        "momentum_pct": external.get("momentum_pct"),
        "long_momentum_pct": external.get("long_momentum_pct"),
        "volume_ratio": external.get("volume_ratio"),
        "pressure": external.get("pressure") if external.get("pressure_available") else None,
        "data_source": external.get("source"),
        "candle_age_seconds": external.get("candle_age_seconds"),
    }
    schema = {"type": "object", "properties": {
        "verdict": {"type": "string", "enum": ["CONFIRM", "CAUTION", "WAIT"]},
        "reason": {"type": "string"},
        "flip_risk": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "UNKNOWN"]}
    }, "required": ["verdict", "reason", "flip_risk"], "additionalProperties": False}
    payload = {
        "model": MODEL, "store": False,
        "instructions": (
            "You are a cautious BTC indicator cross-checker, not a price oracle. "
            "Review only supplied market observations. CONFIRM means indicators are internally "
            "consistent, NOT that a trade will win. CAUTION means conflicting evidence or reversal risk. "
            "WAIT means stale, inadequate or ambiguous data. Never invent order flow, settlement prices, "
            "probabilities, market odds or historical accuracy. Keep reason under 180 characters."
        ),
        "input": json.dumps(snapshot, separators=(",", ":")),
        "text": {"format": {"type": "json_schema", "name": "btc_confirmation",
            "strict": True, "schema": schema}},
        "max_output_tokens": 180,
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
        if parsed.get("verdict") not in ("CONFIRM", "CAUTION", "WAIT"):
            raise ValueError("Invalid model verdict")
        _cache = {"status": "ready", "verdict": parsed["verdict"],
            "reason": str(parsed.get("reason", ""))[:240],
            "flip_risk": parsed.get("flip_risk", "UNKNOWN"),
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
