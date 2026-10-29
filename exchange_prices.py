"""Bounded, read-only public exchange BTC spot comparison.

USD spot prices and USDT-quoted markets are tracked separately.
Ticker receipt time is not necessarily the last execution timestamp.
No quote is substituted for Polymarket's official BRTI target or settlement.
"""
import asyncio
import math
import statistics
import time
from datetime import datetime

EXCHANGES = (
    ("Coinbase", "USD", "https://api.exchange.coinbase.com/products/BTC-USD/ticker"),
    ("Kraken", "USD", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD"),
    ("Bitstamp", "USD", "https://www.bitstamp.net/api/v2/ticker/btcusd/"),
    ("Gemini", "USD", "https://api.gemini.com/v1/pubticker/btcusd"),
    ("Bitfinex", "USD", "https://api-pub.bitfinex.com/v2/ticker/tBTCUSD"),
    ("Binance.US", "USD", "https://api.binance.us/api/v3/ticker/price?symbol=BTCUSD"),
    ("Binance Global", "USDT", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"),
    ("OKX", "USDT", "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT"),
)

def positive(value):
    try:
        val=float(value)
        return val if math.isfinite(val) and val>0 else None
    except (ValueError,TypeError,OverflowError):
        return None

def parse_response(name, body):
    """Return last price, optional trade epoch. Never infer a trade timestamp."""
    stamp=None
    if name=="Coinbase":
        value=body.get("price")
        raw=body.get("time")
        if raw:
            try: stamp=datetime.fromisoformat(str(raw).replace("Z","+00:00")).timestamp()
            except (ValueError,TypeError): pass
    elif name=="Kraken":
        if body.get("error"): raise ValueError("Kraken error")
        item=next(iter(body["result"].values()))
        value=item["c"][0]
    elif name=="Bitstamp":
        value=body.get("last")
        stamp=positive(body.get("timestamp"))
    elif name=="Gemini":value=body.get("last")
    elif name=="Bitfinex":value=body[6]
    elif name in ("Binance.US","Binance Global"):value=body.get("price")
    elif name=="OKX":
        data=body.get("data") or []
        value=data[0]["last"]
        stamp=positive(data[0].get("ts"))
        if stamp is not None:stamp/=1000
    else:raise ValueError("Unknown source")
    price=positive(value)
    if price is None:raise ValueError("Invalid ticker price")
    return price,stamp

async def fetch_price(client, name, quote, url):
    try:
        response=await client.get(url,timeout=5)
        response.raise_for_status()
        price,trade_epoch=parse_response(name,response.json())
        now=time.time()
        if trade_epoch is not None and (trade_epoch>now+10 or now-trade_epoch>60):
            return {"source":name,"quote":quote,"status":"stale_trade",
                    "reason":"Last execution timestamp is outside the 60-second freshness window"}
        return {"source":name,"quote":quote,"status":"responding",
                "price":round(price,2),"fetched_epoch":round(now,2),
                "trade_age_seconds":round(now-trade_epoch,1) if trade_epoch is not None else None,
                "trade_timestamp_verified":trade_epoch is not None}
    except Exception as exc:
        return {"source":name,"quote":quote,"status":"unavailable","reason":type(exc).__name__}

def summarize_prices(rows, now=None):
    now=time.time() if now is None else now
    normalized=[]
    for row in rows:
        item=dict(row)
        price=positive(item.get("price"))
        stamp=positive(item.get("fetched_epoch"))
        eligible=item.get("status")=="responding" and price is not None and stamp is not None and -5<=now-stamp<=45
        if not eligible:
            item.pop("price",None)
            if item.get("status")=="responding":item["status"]="stale"
        normalized.append(item)
    usd=[x["price"] for x in normalized if x.get("quote")=="USD" and x.get("status")=="responding"]
    usdt=[x["price"] for x in normalized if x.get("quote")=="USDT" and x.get("status")=="responding"]
    def group(prices,quote):
        if not prices:return {"quote":quote,"status":"unavailable","source_count":0}
        med=statistics.median(prices)
        # Outliers remain shown individually, but cannot distort the consensus.
        close=[p for p in prices if abs(p/med-1)<=0.015]
        if not close:return {"quote":quote,"status":"insufficient","source_count":0}
        median=statistics.median(close)
        return {"quote":quote,"status":"available" if len(close)>=3 else "limited",
                "source_count":len(close),"median":round(median,2),
                "low":round(min(close),2),"high":round(max(close),2),
                "spread_usd":round(max(close)-min(close),2),
                "spread_bps":round(10000*(max(close)-min(close))/median,2)}
    group_usd=group(usd,"USD")
    group_usdt=group(usdt,"USDT")
    for row in normalized:
        if row.get("status")!="responding":continue
        median=(group_usd if row["quote"]=="USD" else group_usdt).get("median")
        if median:
            row["deviation_usd"]=round(row["price"]-median,2)
            row["deviation_bps"]=round(10000*(row["price"]-median)/median,2)
            if abs(row["price"]/median-1)>.015:row["status"]="outlier"
    return {"status":"available" if group_usd.get("status")=="available" else "limited" if usd else "unavailable",
            "as_of_epoch":now,"refresh_seconds":15,"usd":group_usd,"usdt":group_usdt,
            "exchanges":normalized,"coverage":"Six USD spot venues and two USDT-quoted venues are requested; each can be unavailable.",
            "limitations":"Responses from venues without trade timestamps verify HTTP response time, not last trade freshness. USDT is not assumed to equal USD; venue prices are not the Polymarket BRTI settlement index. No global volume weighting or arbitrage guarantee."}

async def fetch_snapshot(client):
    rows=await asyncio.gather(*(fetch_price(client,*spec) for spec in EXCHANGES))
    return summarize_prices(rows)

def public_snapshot(snapshot, now=None):
    now=time.time() if now is None else now
    age=now-float(snapshot.get("as_of_epoch") or 0)
    if age<=0 or age>45:
        stale=dict(snapshot)
        stale.update(status="stale",age_seconds=round(max(0,age),1))
        stale["usd"]={"status":"stale","source_count":0}
        stale["usdt"]={"status":"stale","source_count":0}
        stale["exchanges"]=[{**{k:v for k,v in x.items() if k!="price"},"status":"stale" if x.get("status")=="responding" else x.get("status")} for x in snapshot.get("exchanges",[])]
        return stale
    return {**snapshot,"age_seconds":round(age,1)}
