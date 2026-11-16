"""One-shot, read-only Polymarket US WebSocket handshake diagnostic.

Never logs credentials, signatures, raw frames or exceptions.
No order submission, no mutation of prediction or BRTI data.
"""
import asyncio
import json
import os
import time

from polymarket_us_stream import URL, auth_headers, subscription, parse_quote

_status = {"status": "pending", "stage": "waiting_for_market"}
_ALLOWED = {"missing_credentials", "invalid_credentials_format", "connection_failed",
            "subscription_failed", "no_messages", "no_valid_quotes", "success",
            "market_unavailable", "unexpected_error"}


def snapshot():
    return dict(_status)


async def run_once(market_provider, wait_seconds=90, listen_seconds=12):
    global _status
    _status = {"status": "running", "stage": "waiting_for_market"}
    market = None
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        candidate = market_provider()
        if isinstance(candidate, str) and candidate.startswith(("btc-updown-", "cpc-btc-updown-")):
            market = candidate
            break
        await asyncio.sleep(2)
    if not market:
        _status = {"status": "unavailable", "reason": "market_unavailable"}
        return
    key_id = os.getenv("POLYMARKET_KEY_ID")
    secret = os.getenv("POLYMARKET_SECRET_KEY")
    if not key_id or not secret:
        _status = {"status": "failed", "reason": "missing_credentials"}
        return
    try:
        headers = auth_headers(key_id, secret)
    except (ValueError, TypeError, ImportError):
        _status = {"status": "failed", "reason": "invalid_credentials_format"}
        return
    except Exception:
        _status = {"status": "failed", "reason": "invalid_credentials_format"}
        return

    from websockets.asyncio.client import connect
    start = time.monotonic()
    stage = "connecting"
    received = 0
    valid = 0
    try:
        async with connect(URL, additional_headers=headers, open_timeout=8,
                           close_timeout=2, ping_interval=20, max_size=262144) as ws:
            handshake_ms = round((time.monotonic() - start) * 1000)
            stage = "subscribing"
            await ws.send(json.dumps(subscription([market])))
            stage = "receiving"
            end = time.monotonic() + listen_seconds
            while time.monotonic() < end:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=min(3, max(.1, end-time.monotonic())))
                except asyncio.TimeoutError:
                    continue
                received += 1
                try:
                    if parse_quote(frame, [market]) is not None:
                        valid += 1
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
            _status = {"status": "success" if valid else "inconclusive",
                       "reason": None if valid else ("no_messages" if not received else "no_valid_quotes"),
                       "handshake_ms": handshake_ms, "messages_received": received,
                       "valid_quotes": valid, "market": market}
    except asyncio.CancelledError:
        raise
    except Exception:
        # Intentionally do not reveal server exception strings, frames or headers.
        reason = "connection_failed" if stage == "connecting" else "subscription_failed"
        _status = {"status": "failed", "reason": reason, "stage": stage}
