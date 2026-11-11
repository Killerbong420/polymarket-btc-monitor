"""Bounded, read-only exchange trade streams. Never used as a BRTI settlement source.

Live observations are partial public execution samples, not full exchange volume.
Drops, reconnects and insufficient window coverage always fail closed.
"""
import asyncio
import json
import math
import time
from collections import deque
from datetime import datetime

from websockets.asyncio.client import connect
from coinbase_live_candles import CoinbaseMinuteBuilder

ENDPOINTS = {
    "Coinbase": "wss://ws-feed.exchange.coinbase.com",
    "Kraken": "wss://ws.kraken.com/v2",
}
MAX_TRADES = 6000
RETENTION_SECONDS = 180
FRESH_SECONDS = 10


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def parse_trade_events(source, message):
    """Normalize execution timestamp/price/BTC size/initiator side/trade id."""
    if not isinstance(message, dict):
        return []
    if source == "Coinbase":
        # Coinbase side is the maker side; invert to identify initiator.
        if message.get("type") != "match" or message.get("product_id") != "BTC-USD":
            return []
        return [(
            _epoch(message.get("time")), message.get("price"),
            message.get("size"),
            {"sell": "buy", "buy": "sell"}.get(message.get("side")),
            message.get("trade_id"),
        )]
    if source == "Kraken":
        if message.get("channel") != "trade" or message.get("type") != "update":
            return []  # Subscribed snapshots are NOT new trades.
        data = message.get("data")
        if not isinstance(data, list):
            return []
        return [(
            _epoch(item.get("timestamp")), item.get("price"),
            item.get("qty"), item.get("side"), item.get("trade_id"),
        ) for item in data if isinstance(item, dict) and item.get("symbol") == "BTC/USD"]
    return []


class TradeBuffer:
    """One producer / event-loop readers; bounded by time and count."""
    def __init__(self):
        self.rows = deque()
        self.ids = set()
        self.connected_since = None
        self.latest_trade_epoch = None
        self.overflow_epoch = None
        self.last_error = None

    def connected(self, now):
        self.rows.clear()
        self.ids.clear()
        self.connected_since = now
        self.latest_trade_epoch = None
        self.overflow_epoch = None
        self.last_error = None

    def disconnected(self, reason=None):
        # A reconnect starts a new observation window. Never bridge missing time.
        self.rows.clear()
        self.ids.clear()
        self.connected_since = None
        self.latest_trade_epoch = None
        self.overflow_epoch = None
        self.last_error = reason

    def _prune(self, now):
        while self.rows and self.rows[0][0] < now - RETENTION_SECONDS:
            self.ids.discard(self.rows.popleft()[4])

    def append(self, event, now=None):
        now = time.time() if now is None else now
        try:
            ts, price, size, side, ident = event
            ts, price, size = float(ts), float(price), float(size)
            if not all(math.isfinite(x) for x in (ts, price, size)):
                return False
            if price <= 0 or size <= 0 or ts < now-RETENTION_SECONDS or ts > now+2:
                return False
            if side not in ("buy", "sell") or ident is None:
                return False
            ident = str(ident)
            if len(ident) > 128:
                return False
        except (TypeError, ValueError, OverflowError):
            return False
        if self.connected_since is None:
            return False
        self._prune(now)
        if ident in self.ids:
            return False
        if len(self.rows) >= MAX_TRADES:
            self.ids.discard(self.rows.popleft()[4])
            self.overflow_epoch = now
        self.rows.append((ts, price, size, side, ident))
        self.ids.add(ident)
        self.latest_trade_epoch = max(ts, self.latest_trade_epoch or ts)
        return True

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        self._prune(now)
        if (self.connected_since is None or self.latest_trade_epoch is None
                or now-self.latest_trade_epoch > FRESH_SECONDS
                or now-self.latest_trade_epoch < -2):
            return None
        return {
            "rows": list(self.rows),
            "connected_since_epoch": self.connected_since,
            "overflow_epoch": self.overflow_epoch,
            "latest_trade_epoch": self.latest_trade_epoch,
        }


BUFFERS = {source: TradeBuffer() for source in ENDPOINTS}
# One small OHLC aggregator shares the EXISTING Coinbase trades + heartbeat
# WebSocket. No second WebSocket, and no extra external REST calls.
COINBASE_MINUTES = CoinbaseMinuteBuilder()


def coinbase_candle_snapshot(now=None):
    return COINBASE_MINUTES.snapshot(now)



def stream_snapshot(source, now=None):
    buf = BUFFERS.get(source)
    return buf.snapshot(now) if buf else None


async def stream_exchange(source):
    """Reconnect with bounded backoff; never spawn new tasks per message."""
    buffer = BUFFERS[source]
    delay = 1
    while True:
        started = time.monotonic()
        try:
            async with connect(
                ENDPOINTS[source], open_timeout=8, ping_interval=20,
                ping_timeout=15, close_timeout=3, max_size=524288, max_queue=16,
            ) as socket:
                if source == "Coinbase":
                    subscribe = {"type": "subscribe", "product_ids": ["BTC-USD"],
                                 "channels": ["matches", "heartbeat"]}
                else:
                    subscribe = {"method": "subscribe", "params": {
                        "channel": "trade", "symbol": ["BTC/USD"], "snapshot": False}}
                await socket.send(json.dumps(subscribe))
                current=time.time()
                buffer.connected(current)
                if source=="Coinbase":
                    COINBASE_MINUTES.connected(current)
                async for raw in socket:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        continue
                    if (message.get("type") == "error"
                            or message.get("success") is False):
                        raise ValueError("Exchange subscription rejected")
                    now = time.time()
                    if source=="Coinbase" and message.get("type")=="heartbeat":
                        COINBASE_MINUTES.heartbeat(message,now=now)
                    for event in parse_trade_events(source, message):
                        if buffer.append(event, now=now) and source=="Coinbase":
                            COINBASE_MINUTES.append(event,now=now)
        except asyncio.CancelledError:
            buffer.disconnected("stopped")
            if source=="Coinbase":
                COINBASE_MINUTES.disconnected("stopped")
            raise
        except Exception as exc:
            buffer.disconnected(type(exc).__name__)
            if source=="Coinbase":
                COINBASE_MINUTES.disconnected(type(exc).__name__)
        else:
            buffer.disconnected("connection_closed")
            if source=="Coinbase":
                COINBASE_MINUTES.disconnected("connection_closed")
        if time.monotonic()-started >= 20:
            delay = 1
        else:
            delay = min(30, delay * 2)
        await asyncio.sleep(delay)


async def stream_loop():
    tasks = [asyncio.create_task(stream_exchange(source)) for source in ENDPOINTS]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
