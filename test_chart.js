const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/terminal.html','utf8');
const scripts=[...html.matchAll(/<script[^>]*>([\\s\\S]*?)<\\/script>/g)].map(x=>x[1]);
assert.ok(scripts.length>=4);
for(const script of scripts)new vm.Script(script);
for(const id of ['candleChart','priceChart','brtiCanvas']){
 assert.ok(!html.includes('id="'+id+'"'),id+' must not be mounted');
}
for(const endpoint of ['/api/candles','/api/brti-chart']){
 assert.ok(!html.includes("fetch('"+endpoint+"'"),endpoint+' must not be polled by the browser');
}
assert.match(html,/setInterval\\(\\(\\)=>\\{if\\(!document.hidden\\)updateServer\\(\\)\\},2000\\)/);
assert.match(html,/setInterval\\(\\(\\)=>\\{if\\(!document.hidden\\)refreshFive\\(\\)\\},2000\\)/);
assert.match(html,/if\\(statusInFlight\\)return/);
assert.match(html,/if\\(fiveBusy\\|\\|document.hidden\\)return/);
assert.match(html,/finally\\{clearTimeout\\(timeout\\);fiveBusy=false;\\}/);
assert.match(html,/setInterval\\(\\(\\)=>\\{if\\(!document.hidden\\)refreshRoundTiles\\(\\)\\},10000\\)/);
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
console.log('PASS: chart-free dashboard JS, guarded 2s live polling, and official BRTI target freshness');
