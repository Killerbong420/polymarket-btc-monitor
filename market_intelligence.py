"""Timestamped public exchange samples and publisher headlines for research."""
import asyncio
import copy
import math
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import httpx
from exchange_prices import fetch_snapshot, public_snapshot
from market_chart_history import record_exchange
from live_trade_stream import stream_snapshot

FEEDS = [('CoinDesk','https://www.coindesk.com/arc/outboundfeeds/rss/'),
         ('Cointelegraph','https://cointelegraph.com/rss'),
         ('Federal Reserve','https://www.federalreserve.gov/feeds/press_all.xml')]
_state = {'trade_flow':{'status':'starting','exchanges':[]},
          'order_book':{'status':'starting'},'news':{'status':'starting','headlines':[]},
          'price_comparison':{'status':'starting','exchanges':[], 'as_of_epoch':0}}

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

def short_window_pressure(rows, now, seconds):
    """Observed initiated volume; incomplete snapshots must not imply full coverage."""
    sample=[(t,p*q,side) for t,p,q,side in rows if now-seconds<=t<=now]
    latest=max((t for t,_,_ in sample),default=None)
    earliest=min((t for t,_,_ in sample),default=None)
    buy=sum(value for _,value,side in sample if side=='buy')
    sell=sum(value for _,value,side in sample if side=='sell')
    # An exchange's most-recent-N API can omit older trades in a busy market.
    # Require observed samples spanning most of the window before labeling coverage.
    covered=bool(sample and earliest<=now-seconds+3 and latest>=now-5)
    return {'window_seconds':seconds,'status':'observed' if covered else 'partial',
            'buy_usd':round(buy,2),'sell_usd':round(sell,2),
            'imbalance':round((buy-sell)/(buy+sell),4) if buy+sell else None,
            'trade_count':len(sample),'sample_span_seconds':round(latest-earliest,1) if sample else 0,
            'latest_age_seconds':round(now-latest,1) if sample else None,
            'coverage':'sample spans near both window edges' if covered else 'insufficient time coverage; not global volume'}

def summarize_trades(trades, source, now):
    """Normalized (epoch, price, BTC quantity, taker side, id) observations."""
    valid={}
    for ts,price,qty,side,ident in trades:
        try:
            ts,price,qty=float(ts),float(price),float(qty)
            if not all(math.isfinite(x) for x in (ts,price,qty)): continue
            if not now-180<=ts<=now+2 or price<=0 or qty<=0 or side not in ('buy','sell'): continue
            valid[str(ident)]=(ts,price,qty,side)
        except (TypeError,ValueError): continue
    rows=list(valid.values())
    if not rows: return {'source':source,'status':'unavailable','reason':'No valid trades in the last 180 seconds'}
    latest=max(x[0] for x in rows)
    buys=sum(p*q for t,p,q,s in rows if s=='buy')
    sells=sum(p*q for t,p,q,s in rows if s=='sell')
    return {'source':source,'status':'fresh' if now-latest<=45 else 'stale',
            'buy_usd':round(buys,2),'sell_usd':round(sells,2),
            'buy_btc':round(sum(q for t,p,q,s in rows if s=='buy'),8),
            'sell_btc':round(sum(q for t,p,q,s in rows if s=='sell'),8),
            'imbalance':round((buys-sells)/(buys+sells),4),
            'trade_count':len(rows),'latest_trade_epoch':latest,
            'short_windows':{'10s':short_window_pressure(rows,now,10),
                             '30s':short_window_pressure(rows,now,30)},
            'sample_start':iso(min(x[0] for x in rows)), 'sample_end':iso(latest),
            'sample_seconds':round(latest-min(x[0] for x in rows),1),
            'note':'Observed recent trade sample, not complete exchange or global volume; every trade has a buyer and seller. Side identifies the initiating order.'}

async def exchange_flow(client, source):
    now=time.time()
    try:
        if source=='Coinbase':
            r=await client.get('https://api.exchange.coinbase.com/products/BTC-USD/trades',params={'limit':1000},timeout=6)
            r.raise_for_status()
            rows=[]
            for t in r.json():
                try:
                    ts=datetime.fromisoformat(t['time'].replace('Z','+00:00')).timestamp()
                    # Coinbase's side describes the maker; invert to taker.
                    side={'sell':'buy','buy':'sell'}.get(t.get('side'))
                    rows.append((ts,t['price'],t['size'],side,t['trade_id']))
                except (TypeError,ValueError,KeyError): continue
        else:
            r=await client.get('https://api.kraken.com/0/public/Trades',params={'pair':'XBTUSD','count':1000},timeout=6)
            r.raise_for_status();body=r.json()
            if body.get('error'): raise ValueError('Exchange error')
            key=next(k for k in body['result'] if k!='last')
            rows=[(t[2],t[0],t[1],{'b':'buy','s':'sell'}.get(t[3]),t[6] if len(t)>6 else tuple(t))
                  for t in body['result'][key] if isinstance(t,list) and len(t)>=4]
        return summarize_trades(rows,source,time.time())
    except Exception as exc:
        return {'source':source,'status':'unavailable','reason':type(exc).__name__}

def streaming_flow(source, now=None):
    """Use only currently connected, fresh live executions; report gaps honestly."""
    now = time.time() if now is None else now
    sample = stream_snapshot(source, now)
    if not sample:
        return None
    result = summarize_trades(sample["rows"], source, now)
    if result["status"] != "fresh":
        return None
    result["transport"] = "websocket"
    result["connected_since_epoch"] = sample["connected_since_epoch"]
    result["latest_trade_age_seconds"] = round(now - sample["latest_trade_epoch"], 2)
    for seconds in (10, 30):
        window = result["short_windows"][str(seconds)+"s"]
        overflow = sample["overflow_epoch"]
        if (now - sample["connected_since_epoch"] < seconds + 3
                or (overflow is not None and now - overflow <= seconds)
                or now - sample["latest_trade_epoch"] > 5):
            window["status"] = "partial"
            window["coverage"] = "stream warming, over capacity, or latest trade too old; not complete volume"
    result["note"] = ("Partial, bounded live exchange execution sample, not the official BRTI or complete/global volume. "
                      "WebSocket channels may drop trades without notice; observed windows describe coverage only.")
    return result


async def preferred_exchange_flow(client, source):
    live = streaming_flow(source)
    if live is not None:
        return live
    fallback = await exchange_flow(client, source)
    fallback["transport"] = "rest_sample"
    return fallback


def public_trade_flow(now=None):
    """Instant read of stream buffers, falling back to last REST observations."""
    now = time.time() if now is None else now
    flow = copy.deepcopy(_state["trade_flow"])
    by_source = {row.get("source"): row for row in flow.get("exchanges", [])}
    any_stream = False
    for source in ("Coinbase", "Kraken"):
        live = streaming_flow(source, now)
        if live is not None:
            by_source[source] = live
            any_stream = True
    rows = [by_source.get(source) for source in ("Coinbase", "Kraken")]
    flow["exchanges"] = [x for x in rows if isinstance(x, dict)]
    for item in flow["exchanges"]:
        ts = item.get("latest_trade_epoch")
        item["age_seconds"] = round(max(0, now-ts)) if ts is not None else None
        if ts is not None and now-ts > 45:
            item["status"] = "stale"
    flow["status"] = ("available" if any(x.get("status") == "fresh" for x in flow["exchanges"])
                      else "unavailable")
    flow["transport"] = "websocket_with_rest_fallback" if any_stream else "rest_fallback"
    if any_stream:
        flow["fetched_epoch"] = now
    last_fetch = flow.get("fetched_epoch")
    flow["age_seconds"] = round(max(0, now-last_fetch)) if last_fetch else None
    if last_fetch and now-last_fetch > 60:
        flow["status"] = "stale"
    return flow


async def order_book(client):
    try:
        r=await client.get('https://api.kraken.com/0/public/Depth',params={'pair':'XBTUSD','count':20},timeout=6)
        r.raise_for_status();body=r.json()
        if body.get('error'): raise ValueError('Exchange error')
        book=next(iter(body['result'].values()))
        bids=sum(float(x[0])*float(x[1]) for x in book['bids'][:20])
        asks=sum(float(x[0])*float(x[1]) for x in book['asks'][:20])
        if not math.isfinite(bids+asks) or bids+asks<=0: raise ValueError('Empty book')
        return {'status':'fresh','source':'Kraken BTC/USD','levels_per_side':20,
                'bid_usd':round(bids,2),'ask_usd':round(asks,2),
                'imbalance':round((bids-asks)/(bids+asks),4),'fetched_epoch':time.time(),
                'note':'Displayed resting liquidity, not executed buys/sells; orders can be cancelled.'}
    except Exception as exc:return {'status':'unavailable','source':'Kraken BTC/USD','reason':type(exc).__name__}

def parse_headlines(xml,source,now):
    root=ET.fromstring(xml)
    items=[]
    for item in root.findall('.//item')[:100]:
        title=' '.join((item.findtext('title') or '').split())
        link=(item.findtext('link') or '').strip()
        try:
            stamp=parsedate_to_datetime(item.findtext('pubDate') or '')
            if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
            ts=stamp.timestamp()
        except (TypeError,ValueError,OverflowError):continue
        if not now-86400<=ts<=now+60:continue
        if urlparse(link).scheme not in ('https','http'):continue
        if source!='Federal Reserve' and not re.search(r'\b(bitcoin|btc|crypto|fed|federal reserve|inflation|interest rate|treasury|etf|sec|hack|war|tariff|liquidation)\b',title,re.I):continue
        items.append({'title':' '.join(title.split()[:24])[:180], 'url':link,
                      'source':source,'published_at':iso(ts),'published_epoch':ts,
                      'verification':'Publisher headline only; article and causal price impact not independently verified'})
    return items

def classify_headline(item, now):
    """Editorial labels are heuristic; a headline is not independently verified fact."""
    title=item['title'];lower=title.lower();age=max(0,now-item['published_epoch'])
    analysis=bool(re.search(r"\b(opinion|analysis|outlook|predicts?|forecast|could|may|might|why|what if)\b",lower))
    category='BITCOIN' if re.search(r'\b(bitcoin|btc)\b',lower) else 'MACRO' if re.search(r'\b(fed|inflation|treasury|interest rate|war|tariff)\b',lower) else 'REGULATION' if re.search(r'\b(sec|ban|regulat|bank)',lower) else 'CRYPTO'
    date_match=re.search(r'/(20\d{2})/(\d{2})/(\d{2})/',item['url'])
    old_url=False
    if date_match:
        try:
            url_date=datetime(*map(int,date_match.groups()),tzinfo=timezone.utc).timestamp()
            old_url=now-url_date>172800
        except ValueError:pass
    item.update(id=hashlib.sha256(item['url'].encode()).hexdigest()[:16],
                age_seconds=round(age),category=category,
                content_type='COMMENTARY_OR_FORECAST' if analysis else 'PUBLISHER_REPORT_UNVERIFIED',
                freshness='OLDER_URL_OR_REPUBLISHED' if old_url else 'RECENT_PUBLICATION' if age<=5400 else 'BACKGROUND',
                event_time_status='UNKNOWN; publication time is not confirmed event time',
                possible_republication=old_url)
    return item

def select_headlines(headlines,now):
    candidates=[classify_headline(dict(x),now) for x in headlines]
    candidates.sort(key=lambda x:(x['freshness']=='RECENT_PUBLICATION',x['published_epoch']),reverse=True)
    chosen=[]
    for item in candidates:
        tokens=set(re.findall(r'[a-z0-9]+',item['title'].lower()))-{'the','a','an','and','of','to','in','on','for','as','with'}
        duplicate=None
        for prev in chosen:
            other=set(re.findall(r'[a-z0-9]+',prev['title'].lower()))-{'the','a','an','and','of','to','in','on','for','as','with'}
            if item['url']==prev['url'] or len(tokens&other)/max(1,len(tokens|other))>=.65:
                duplicate=prev;break
        if duplicate:
            if item['source'] not in duplicate['also_reported_by']:duplicate['also_reported_by'].append(item['source'])
            continue
        item['also_reported_by']=[];chosen.append(item)
    return chosen[:8]

async def news_feed(client, source, url):
    try:
        r=await client.get(url,timeout=8,follow_redirects=True)
        r.raise_for_status()
        if len(r.content)>2_000_000:raise ValueError('Feed too large')
        items=parse_headlines(r.content,source,time.time())
        return items,{'source':source,'status':'ok','eligible_headlines':len(items)}
    except Exception as exc:return [],{'source':source,'status':'unavailable','reason':type(exc).__name__}

async def refresh(client, include_news=True):
    jobs=[preferred_exchange_flow(client,'Coinbase'),preferred_exchange_flow(client,'Kraken'),order_book(client),fetch_snapshot(client)]
    if include_news:jobs.extend(news_feed(client,s,u) for s,u in FEEDS)
    results=await asyncio.gather(*jobs)
    now=time.time()
    _state['trade_flow']={'status':'available' if any(x['status']=='fresh' for x in results[:2]) else 'unavailable',
                           'exchanges':results[:2],'fetched_epoch':now,'window_seconds':180}
    _state['order_book']=results[2]
    _state['price_comparison']=results[3]
    record_exchange(results[3],now)
    if include_news:
        items={};feeds=[]
        for headlines,health in results[4:]:
            feeds.append(health)
            for item in headlines:
                key=re.sub(r'\W+','',item['title'].lower())
                if key not in items or item['published_epoch']>items[key]['published_epoch']:items[key]=item
        headlines=select_headlines(list(items.values()),now)
        ok=sum(x['status']=='ok' for x in feeds)
        _state['news']={'status':'available' if ok==len(FEEDS) else 'partial' if ok else 'unavailable',
                        'headlines':headlines,'feeds':feeds,'fetched_epoch':now,
                        'lookback_hours':24,'refresh_seconds':120,
                        'note':'Selected publisher RSS headlines, not comprehensive real-time news. Publication time may differ from event time. Empty results do not prove no breaking news.'}

def public_intelligence():
    d=copy.deepcopy(_state);now=time.time()
    d['price_comparison']=public_snapshot(d.get('price_comparison') or {},now)
    d['trade_flow']=public_trade_flow(now)
    for key in ('order_book','news'):
        item=d[key];ts=item.get('fetched_epoch')
        item['age_seconds']=round(max(0,now-ts)) if ts else None
        if ts and now-ts>(300 if key=='news' else 60):item['status']='stale'
    d['coverage']='BTC/USD last prices from Coinbase, Kraken, Bitstamp, Gemini, Bitfinex, Binance.US; Binance Global and OKX BTC/USDT shown separately. Coinbase/Kraken live observed trade flow with REST fallback, Kraken depth, and selected news. WebSocket gaps are possible; unavailable venues remain labeled; no full global trade-flow or on-chain coverage.'
    return d

async def intelligence_loop():
    async with httpx.AsyncClient(headers={'User-Agent':'BTCResearchMonitor/1.0'}) as client:
        next_news=0
        while True:
            started=time.monotonic();include_news=started>=next_news
            try:
                await refresh(client,include_news)
                if include_news:next_news=started+120
            except Exception:
                # Existing timestamps make old observations visibly stale; never stop monitoring.
                pass
            await asyncio.sleep(max(1,15-(time.monotonic()-started)))
