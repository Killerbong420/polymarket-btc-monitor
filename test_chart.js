const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/terminal.html','utf8');
const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]);
assert.ok(scripts.length>=4);
for(const script of scripts)new vm.Script(script);
for(const id of ['candleChart','priceChart','brtiCanvas']){
 assert.ok(!html.includes('id="'+id+'"'),id+' must not be mounted');
}
for(const endpoint of ['/api/candles','/api/brti-chart']){
 assert.ok(!html.includes("fetch('"+endpoint+"'"),endpoint+' must not be polled by the browser');
}
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)updateServer\(\)\},2000\)/);
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)refreshFive\(\)\},2000\)/);
assert.match(html,/if\(statusInFlight\)return/);
assert.match(html,/if\(fiveBusy\|\|document.hidden\)return/);
assert.match(html,/finally\{clearTimeout\(timeout\);fiveBusy=false;\}/);
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)refreshRoundTiles\(\)\},10000\)/);
const summaryScript=scripts.find(s=>s.includes('function updatePolymarketSummary(d)'));
assert.ok(summaryScript,'official market summary updater is retained');
const elements=new Map(),listeners={};
const element=id=>{
 if(!elements.has(id))elements.set(id,{textContent:'',className:''});
 return elements.get(id);
};
const win={addEventListener:(event,fn)=>listeners[event]=fn,btcServerStatus:null};
vm.runInNewContext(summaryScript,{window:win,document:{getElementById:element},Date});
assert.equal(typeof listeners['btc-status-updated'],'function');
const now=Date.now()/1000,market='cpc-btc-updown-15m-test';
const active={status:'running',market,updated:new Date().toISOString(),reference_price:{status:'fresh',price:81030,price_epoch:now,window_end:now+600},target_behavior:{status:'available',target:81041}};
win.btcServerStatus=active;
listeners['btc-status-updated']();
assert.equal(element('pmApiFeed').textContent,'LIVE · VERIFIED');
assert.equal(element('pmApiTarget').textContent,'$81,041.00');
assert.equal(element('pmApiPrice').textContent,'$81,030.00');
win.btcServerStatus={...active,reference_price:{...active.reference_price,status:'stale'}};
listeners['btc-status-updated']();
assert.equal(element('pmApiFeed').textContent,'REFERENCE STALE');
assert.equal(element('pmApiPrice').textContent,'—');
win.btcServerStatus={...active,target_behavior:{status:'unavailable',target:null}};
listeners['btc-status-updated']();
assert.equal(element('pmApiFeed').textContent,'TARGET MISSING');
assert.equal(element('pmApiTarget').textContent,'—');

// The full 15m reference path is now a lightweight line, not candle OHLC.
{
 const graphScript=scripts.find(x=>x.includes('window.btcBrtiLineModel=model'));
 assert.ok(graphScript,'line renderer must be included');
 const rendered=[],handlers={},drawListeners={};
 const ctx=new Proxy({}, {get:(_,key)=>(...args)=>rendered.push([key,...args]),set:()=>true});
 const canvas={width:0,height:0,getBoundingClientRect:()=>({width:390,height:225}),getContext:()=>ctx};
 const graphStatus={textContent:''};
 const doc={hidden:false,getElementById:id=>id==='brtiLineCanvas'?canvas:id==='brtiLineStatus'?graphStatus:null,
  addEventListener:(name,fn)=>{drawListeners[name]=fn;}};
 const browser={devicePixelRatio:1,btcServerStatus:null,
  addEventListener:(name,fn)=>{handlers[name]=fn;}};
 vm.runInNewContext(graphScript,{window:browser,document:doc,Date});
 assert.equal(typeof browser.btcBrtiLineModel,'function');
 assert.equal(typeof handlers['btc-status-updated'],'function');
 const nowSec=Date.now()/1000, start=Math.floor(nowSec/900)*900, end=start+900;
 const pts=Array.from({length:15},(_,i)=>[nowSec-56+i*3.8,81020+i*1.2]);
 const m='cpc-btc-updown-15m-test';
 const obj={status:'running',market:m,updated:new Date().toISOString(),
  reference_price:{status:'fresh',price_epoch:nowSec-2,window_start:start,window_end:end},
  brti_chart:{market:m,sample_age_seconds:2,points:pts},
  target_behavior:{status:'available',target:81041},
  settlement_forecast:{status:'available',target_usd:81041,forecast_final_mean_usd:81045,
   seconds_to_settlement:end-nowSec}};
 const model=browser.btcBrtiLineModel;
 assert.equal(model(obj,nowSec).target,81041);
 assert.equal(model(obj,nowSec).projected,81045);
 assert.equal(model({...obj,brti_chart:{...obj.brti_chart,market:'old'}},nowSec),null);
 assert.equal(model({...obj,reference_price:{...obj.reference_price,status:'stale'}},nowSec),null);
 assert.equal(model({...obj,target_behavior:{status:'unavailable',target:null}},nowSec).target,null);
 assert.equal(model({...obj,settlement_forecast:{...obj.settlement_forecast,target_usd:80000}},nowSec).projected,null);
 assert.equal(model(obj,end),null);
 browser.btcServerStatus=obj;
 handlers['btc-status-updated']();
 assert.match(graphStatus.textContent,/LIVE/);
 assert.ok(rendered.some(x=>x[0]==='lineTo'),'line chart must draw an index path');
 assert.ok(rendered.every(x=>x.every(v=>typeof v!=='number'||Number.isFinite(v))),'all plotted values must be finite');
 browser.btcServerStatus={...obj,reference_price:{...obj.reference_price,status:'stale'}};
 handlers.resize();
 assert.match(graphStatus.textContent,/stale/);
 assert.ok(!html.includes('ENTRY COST SCENARIO'));
 assert.ok(!html.includes('id="costCalculate"'));
 console.log('PASS: real BRTI line, official price-to-beat, forecast guard, stale/rollover handling, no cost controls');
}

console.log('PASS: chart-light dashboard JS, guarded 2s live polling, and official BRTI target freshness');
