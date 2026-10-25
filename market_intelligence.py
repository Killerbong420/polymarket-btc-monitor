"""Timestamped public exchange samples and publisher headlines for research."""
import asyncio
import copy
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import httpx

FEEDS = [('CoinDesk','https://www.coindesk.com/arc/outboundfeeds/rss/'),
         ('Cointelegraph','https://cointelegraph.com/rss'),
         ('Federal Reserve','https://www.federalreserve.gov/feeds/press_all.xml')]
_state = {'trade_flow':{'status':'starting','exchanges':[]},
          'order_book':{'status':'starting'},'news':{'status':'starting','headlines':[]}}

def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()

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

async def news_feed(client, source, url):
    try:
        r=await client.get(url,timeout=8,follow_redirects=True)
        r.raise_for_status()
        if len(r.content)>2_000_000:raise ValueError('Feed too large')
        items=parse_headlines(r.content,source,time.time())
        return items,{'source':source,'status':'ok','eligible_headlines':len(items)}
    except Exception as exc:return [],{'source':source,'status':'unavailable','reason':type(exc).__name__}

async def refresh(client, include_news=True):
    jobs=[exchange_flow(client,'Coinbase'),exchange_flow(client,'Kraken'),order_book(client)]
    if include_news:jobs.extend(news_feed(client,s,u) for s,u in FEEDS)
    results=await asyncio.gather(*jobs)
    now=time.time()
    _state['trade_flow']={'status':'available' if any(x['status']=='fresh' for x in results[:2]) else 'unavailable',
                           'exchanges':results[:2],'fetched_epoch':now,'window_seconds':180}
    _state['order_book']=results[2]
    if include_news:
        items={};feeds=[]
        for headlines,health in results[3:]:
            feeds.append(health)
            for item in headlines:
                key=re.sub(r'\W+','',item['title'].lower())
                if key not in items or item['published_epoch']>items[key]['published_epoch']:items[key]=item
        headlines=sorted(items.values(),key=lambda x:x['published_epoch'],reverse=True)[:8]
        ok=sum(x['status']=='ok' for x in feeds)
        _state['news']={'status':'available' if ok==len(FEEDS) else 'partial' if ok else 'unavailable',
                        'headlines':headlines,'feeds':feeds,'fetched_epoch':now,
                        'lookback_hours':24,'refresh_seconds':120,
                        'note':'Selected publisher RSS headlines, not comprehensive real-time news. Publication time may differ from event time. Empty results do not prove no breaking news.'}

def public_intelligence():
    d=copy.deepcopy(_state);now=time.time()
    for key in ('trade_flow','order_book','news'):
        item=d[key];ts=item.get('fetched_epoch')
        item['age_seconds']=round(max(0,now-ts)) if ts else None
        if ts and now-ts>(300 if key=='news' else 60):item['status']='stale'
    for exchange in d['trade_flow'].get('exchanges',[]):
        ts=exchange.get('latest_trade_epoch')
        exchange['age_seconds']=round(max(0,now-ts)) if ts else None
        if ts and now-ts>45:exchange['status']='stale'
    d['coverage']='Coinbase/Kraken spot trades, Kraken depth, CoinDesk/Cointelegraph/Federal Reserve RSS; no verified on-chain wallet flow, all-exchange flow, or full news coverage.'
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
