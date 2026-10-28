"""Persistent in-app alert transitions; does not send external messages."""
import time
import uuid
from datetime import datetime,timezone
_seen={}
_initialized=False
BOOT=time.time()

def checks(state,ai,db,db_error=None,now=None):
    now=time.time() if now is None else now
    ext=state.get('external') or {};intel=ext.get('external_intelligence') or {};target=state.get('target_behavior') or {}
    try:age=now-datetime.fromisoformat(state['updated']).timestamp()
    except (KeyError,ValueError,TypeError):age=now-BOOT
    results=[]
    def add(code,active,severity,text):
        if active:results.append({'code':code,'severity':severity,'message':text})
    add('LOOP_STALE',age>45,'critical','Monitor has not completed a data cycle in over 45 seconds.')
    add('PAPER_EXIT_UNFILLED',state.get('paper_unfilled_exits',0)>0,'warning','A protective fake-money sell could not fully fill; the engine is retrying. No completed sale is assumed.')
    add('PAPER_ENGINE',bool(state.get('paper_error')),'warning','Paper-trading engine reported an error; no simulated entry should be assumed.')
    add('MONITOR_ERROR',bool(state.get('error')),'critical','Monitoring cycle reported an error; inspect feed and database alerts.')
    add('DATABASE',bool(db_error),'critical','Prediction database is unavailable; saved decisions may be missing.')
    add('PERSISTENCE',not db,'warning','SQLite storage is local; persistent DATABASE_URL is not configured.')
    add('MARKET',state.get('market') is None and now-BOOT>30,'warning','No verified current 15-minute contract.')
    add('SPOT_FEED',ext.get('price') is None or (ext.get('candle_age_seconds') is not None and ext['candle_age_seconds']>100),'warning','Fresh completed exchange candles are unavailable.')
    ref=state.get('reference_price') or {}
    add('REFERENCE_FEED',ref.get('status')!='fresh','warning','Reference index is stale or unavailable; target analysis uses a labeled spot proxy when possible.')
    add('TARGET',target.get('target') is None,'warning','Official price to beat is missing; target-side baseline is unavailable.')
    add('TARGET_MISMATCH',state.get('target_validation',{}).get('status')=='mismatch' and state.get('target_validation',{}).get('material') is True,'warning','Published target differs from the independently reconstructed reference average.')
    add('AI',ai.get('status') in ('error','not_configured'),'warning','AI request failed or API access is not configured.')
    add('AI_STALE',ai.get('status')=='ready' and (ai.get('market')!=state.get('market') or now-ai.get('updated_epoch',0)>300),'warning','AI output belongs to an earlier round or is over five minutes old.')
    for key,code in [('trade_flow','TRADE_FLOW'),('news','NEWS_FEED')]:
        add(code,(intel.get(key) or {}).get('status') not in ('available','partial'),'warning',key.replace('_',' ').title()+' is unavailable or stale.')
    add('PARTIAL_NEWS',(intel.get('news') or {}).get('status')=='partial','warning','Some news publishers are unavailable.')
    add('CHECKPOINT_MISSED',bool(state.get('missed_checkpoints')),'warning','A scheduled checkpoint was missed. The monitor may have started after that checkpoint; no retrospective prediction has been fabricated.')
    add('SETTLEMENT',(state.get('settlement_diagnostic') or {}).get('overdue_markets',0)>0,'warning','Some recorded markets remain unresolved more than 30 minutes after close.')
    return results

def persist_alerts(execute,alerts):
    global _initialized
    if not _initialized:
        _seen.update({code:message if active else None for code,message,active in execute('SELECT code,message,active FROM monitor_alerts',fetch=True)})
        _initialized=True
    active={x['code']:x for x in alerts};now=datetime.now(timezone.utc).isoformat()
    for code in set(active)|set(_seen):
        entry=active.get(code);signature=(entry or {}).get('message')
        if _seen.get(code)==signature:continue
        if entry:
            # One active record per code; transitions survive restarts.
            execute("INSERT INTO monitor_alerts (code,severity,message,active,first_seen,last_changed) VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET severity=excluded.severity,message=excluded.message,active=1,first_seen=CASE WHEN monitor_alerts.active=0 THEN excluded.first_seen ELSE monitor_alerts.first_seen END,last_changed=excluded.last_changed",(code,entry['severity'],entry['message'],1,now,now))
        else:execute('UPDATE monitor_alerts SET active=0,last_changed=? WHERE code=?',(now,code))
        execute('INSERT INTO monitor_events (id,code,severity,message,active,created_at) VALUES (?,?,?,?,?,?)',
                (str(uuid.uuid4()),code,entry['severity'] if entry else 'info',entry['message'] if entry else code+' resolved',1 if entry else 0,now))
        _seen[code]=signature

def alert_history(execute):
    rows=execute('SELECT code,severity,message,active,created_at,created_at FROM monitor_events ORDER BY created_at DESC LIMIT 100',fetch=True)
    return [dict(zip(('code','severity','message','active','first_seen','last_changed'),r)) for r in rows]
