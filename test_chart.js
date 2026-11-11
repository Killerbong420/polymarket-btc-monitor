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
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)updateServer\(\)\},4000\)/);
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)refreshTip\(\)\},3000\)/);
assert.doesNotMatch(html,/\/api\/five-minute|fiveMinutePanel|refreshFive\(/);
assert.match(html,/if\(statusInFlight\)return/);
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)refreshRoundTiles\(\)\},60000\)/);
assert.match(html,/setInterval\(\(\)=>\{if\(!document.hidden\)refreshHistory\(\)\},20000\)/);
assert.doesNotMatch(html,/new WebSocket\(protocol.*exchange-flow/);
assert.match(html,/window\.addEventListener\('btc-status-updated',paint\)/);
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
 let testNow=Math.floor(Date.now()/900000)*900000+450000;
 const TestDate=class extends Date{static now(){return testNow;}};
 vm.runInNewContext(graphScript,{window:browser,document:doc,Date:TestDate});
 assert.equal(typeof browser.btcBrtiLineModel,'function');
 assert.equal(typeof handlers['btc-status-updated'],'function');
 const nowSec=Math.floor(Date.now()/900000)*900+450, start=nowSec-450, end=start+900;
 const pts=Array.from({length:15},(_,i)=>[nowSec-56+i*3.8,81020+i*1.2]);
 const m='cpc-btc-updown-15m-test';
 const obj={status:'running',market:m,updated:new Date(nowSec*1000).toISOString(),
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
 assert.match(graphStatus.textContent,/stale/,'initial empty chart should be marked stale');
 browser.btcServerStatus=obj;
 handlers['btc-status-updated']();
 assert.match(graphStatus.textContent,/LIVE/);
 assert.ok(rendered.some(x=>x[0]==='lineTo'),'line chart must draw an index path');
 assert.ok(rendered.every(x=>x.every(v=>typeof v!=='number'||Number.isFinite(v))),'all plotted values must be finite');
 // During the first minute, the previous 15-minute linear scale squeezed
 // all actual BRTI prices into the left few pixels and repeated the same
 // minute label 4 times. Verify that real observed points now have room.
 const middleNow=testNow,earlyNowSec=start+35;
 const earlyPoints=Array.from({length:15},(_,i)=>[start+3+i*2,81020+i*.4]);
 const early={...obj,updated:new Date(earlyNowSec*1000).toISOString(),
  reference_price:{...obj.reference_price,price_epoch:earlyNowSec-2},
  brti_chart:{...obj.brti_chart,points:earlyPoints,sample_age_seconds:2},
  settlement_forecast:{...obj.settlement_forecast,forecast_final_mean_usd:80900,
   seconds_to_settlement:end-earlyNowSec}};
 testNow=earlyNowSec*1000;
 browser.btcServerStatus=early;rendered.length=0;
 handlers.resize(); // force redraw when simulated time moves backward
 assert.match(graphStatus.textContent,/LIVE/);
 const labels=rendered.filter(x=>x[0]==='fillText');
 assert.ok(labels.some(x=>x[1]==='LIVE DETAIL · ELAPSED TIME EXPANDED'),
   'early round must explicitly disclose time-axis expansion');
 const liveLabel=labels.find(x=>x[1]==='LIVE');
 assert.ok(liveLabel&&liveLabel[2]>140,
   'observed recent BRTI prices must occupy a useful width, not 5% of the chart');
 assert.ok(labels.some(x=>x[1]==='AVG BELOW VIEW'),
   'off-scale projection must be labeled, rather than flattening live prices');
 assert.ok(!labels.some(x=>x[1]==='FUTURE · COMPRESSED')||
           labels.find(x=>x[1]==='FUTURE · COMPRESSED')[2]>100,
   'future annotation must not overflow the live labels');
 const priceBadges=rendered.filter(x=>x[0]==='fillRect'&&x[4]===20);
 assert.equal(priceBadges.length,3,'BRTI, target and forecast retain exact price badges');
 const badgeYs=priceBadges.map(x=>x[2]).sort((a,b)=>a-b);
 assert.ok(badgeYs.every((v,i)=>i===0||v-badgeYs[i-1]>=23.9),
   'price badges must not overlap even if projected and target levels are close');
 assert.ok(rendered.every(x=>x.every(v=>typeof v!=='number'||Number.isFinite(v))),
   'early-round plot must never produce NaN or Infinity');
 testNow=middleNow;
 browser.btcServerStatus={...obj,reference_price:{...obj.reference_price,status:'stale'}};
 handlers.resize();
 assert.match(graphStatus.textContent,/stale/);
 assert.ok(!html.includes('ENTRY COST SCENARIO'));
 assert.ok(!html.includes('id="costCalculate"'));
 console.log('PASS: real BRTI line, official price-to-beat, forecast guard, stale/rollover handling, no cost controls');
}

console.log('PASS: chart-light dashboard JS, guarded 4s status polling, 3s BRTI tips, and official BRTI target freshness');
