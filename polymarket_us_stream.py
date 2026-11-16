"""Read-only Polymarket US market WebSocket adapter.

Opt-in only: never replaces official BRTI targets, outcomes or settlement logic.
"""
import asyncio
import base64
import json
import math
import os
import time
from datetime import datetime, timezone

URL = "wss://api.polymarket.us/v1/ws/markets"
PATH = "/v1/ws/markets"
MAX_AGE_SECONDS = 10


def subscription(slugs, request_id="btc-monitor"):
    valid = list(dict.fromkeys(s for s in slugs if isinstance(s, str) and s.startswith(("btc-updown-", "cpc-btc-updown-")) and len(s) < 110))
    if not valid or len(valid) > 100:
        raise ValueError("No valid BTC market subscriptions")
    return {"subscribe": {"requestId": request_id, "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "marketSlugs": valid}}


def auth_headers(key_id, secret_key, timestamp_ms=None):
    """Sign WebSocket GET handshake; never log secrets or signature."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    if not key_id or not secret_key:
        raise ValueError("Polymarket US credentials missing")
    stamp = str(int(time.time() * 1000) if timestamp_ms is None else int(timestamp_ms))
    private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_key)[:32])
    signed = base64.b64encode(private.sign((stamp + "GET" + PATH).encode())).decode()
    return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": stamp, "X-PM-Signature": signed}


def _price(value):
    if isinstance(value, dict):
        if value.get("currency") != "USD":
            return None
        value = value.get("value")
    try:
        n = float(value)
        return n if math.isfinite(n) and 0 <= n <= 1 else None
    except (TypeError, ValueError, OverflowError):
        return None


def parse_quote(message, subscribed, received_at=None):
    """Only accept open, in-scope, uncrossed quotes. Receive time is not exchange time."""
    if isinstance(message, str):
        message = json.loads(message)
    data = message.get("marketDataLite") if isinstance(message, dict) else None
    if not isinstance(data, dict) or data.get("marketSlug") not in subscribed:
        return None
    bid, ask = _price(data.get("bestBid")), _price(data.get("bestAsk"))
    if bid is None or ask is None or bid > ask:
        return None
    return {"market": data["marketSlug"], "bid": bid, "ask": ask,
            "received_at": time.time() if received_at is None else received_at,
            "source": "Polymarket US authenticated market WebSocket"}


class QuoteCache:
    def __init__(self):
        self._quotes = {}

    def update(self, message, subscribed, received_at=None):
        quote = parse_quote(message, subscribed, received_at)
        if quote:
            self._quotes[quote["market"]] = quote
        return quote

    def get(self, slug, now=None):
        quote = self._quotes.get(slug)
        current = time.time() if now is None else now
        if not quote or not 0 <= current - quote["received_at"] <= MAX_AGE_SECONDS:
            return None
        return dict(quote)

    def clear(self):
        self._quotes.clear()


async def stream_quotes(slug_provider, cache, stop=None):
    """Opt-in reconnecting stream; caller supplies current active market slugs."""
    from websockets.asyncio.client import connect
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret = os.getenv("POLYMARKET_SECRET_KEY")
    if not key_id or not secret:
        return False
    delay = 1
    while stop is None or not stop.is_set():
        try:
            slugs = subscription(slug_provider())["subscribe"]["marketSlugs"]
            headers = auth_headers(key_id, secret)
            async with connect(URL, additional_headers=headers, open_timeout=8,
                               ping_interval=20, ping_timeout=15, max_size=262144) as ws:
                cache.clear()
                await ws.send(json.dumps(subscription(slugs)))
                delay = 1
                while stop is None or not stop.is_set():
                    if set(slug_provider()) != set(slugs):
                        break
                    try:
                        payload = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    cache.update(payload, slugs)
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
        cache.clear()
        await asyncio.sleep(delay)
        delay = min(30, delay * 2)
    return True
