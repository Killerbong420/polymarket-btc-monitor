const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/terminal.html','utf8');
const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(x=>x[1]);
for(const script of scripts)new vm.Script(script);
const script=scripts.find(x=>x.includes("const canvas=document.getElementById('candleChart')"));
const elements=new Map(),listeners={},intervals=[],storage=new Map();
const context=new Proxy({},{get:()=>()=>{}});
function element(id){if(!elements.has(id))elements.set(id,{textContent:'',classList:{toggle(){}},addEventListener(){},getContext:()=>context,parentElement:{getBoundingClientRect:()=>({width:400,height:300})}});return elements.get(id)}
const now=Math.floor(Date.now()/60000)*60000+5000;
const candles=Array.from({length:31},(_,i)=>[(Math.floor(now/60000)-30+i)*60,10000+i*10-2,10000+i*10+12,10000+i*10,10000+i*10+10,0]);
let failed=false,requests=0;
const sandbox={console,AbortController,Date:class extends Date{static now(){return now}},document:{hidden:false,getElementById:element,querySelector:()=>true,addEventListener:(n,f)=>listeners[n]=f},window:{devicePixelRatio:1,addEventListener(){}},sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},setInterval:f=>intervals.push(f),setTimeout:()=>1,clearTimeout(){},fetch:async()=>{requests++;if(failed)throw Error('test feed down');return {ok:true,json:async()=>({status:'fresh',source:'Polymarket US BTC reference',candles})}}};
async function flush(){for(let i=0;i<8;i++)await Promise.resolve()}
(async()=>{
 vm.runInNewContext(script,sandbox);await flush();
 assert.match(element('minuteForecast').textContent,/NEXT 60 SECONDS/);
 assert.match(element('patternDetails').textContent,/Polymarket/);
 assert.ok(storage.has('btcReferenceMinuteCallsV2'));
 const call=JSON.parse(storage.get('btcReferenceMinuteCallsV2'))[0];
 assert.equal(call.target-call.created,60000);
 assert.equal(call.entry,candles.at(-1)[4]);
 failed=true;intervals[0]();await flush();
 assert.match(element('minuteForecast').textContent,/inactive/);
 assert.equal(element('patternName').textContent,'REFERENCE FEED UNAVAILABLE');
 failed=false;listeners.visibilitychange();await flush();
 assert.match(element('minuteForecast').textContent,/NEXT 60 SECONDS/);
 assert.equal(requests,3);
 assert.match(element('minuteAccuracy').textContent,/Directional:/);
 storage.set('btcReferenceMinuteCallsV2',JSON.stringify([null,{created:'invalid'}]));
 listeners.visibilitychange();await flush();
 assert.match(element('minuteForecast').textContent,/NEXT 60 SECONDS/);
 assert.equal(JSON.parse(storage.get('btcReferenceMinuteCallsV2')).length,1);
 console.log('PASS: all inline JS parses; chart render, forecast, feed failure, resume/recovery');
})().catch(e=>{console.error(e);process.exitCode=1});


// Round forecasts must disappear on stale data, mismatched markets and rollover.
{
 const roundScript=scripts.find(s=>s.includes('window.btcRoundChartModel=model'));
 const roundWindow={devicePixelRatio:1},drawing=[],note={};
 const ctx=new Proxy({}, {get:(_,key)=>(...args)=>drawing.push([key,...args])});
 const canvas={parentElement:{getBoundingClientRect:()=>({width:390,height:380})},getContext:()=>ctx};
 const doc={getElementById:id=>id==='chart15'?{classList:{contains:()=>true}}:note};
 vm.runInNewContext(roundScript,{window:roundWindow,document:doc,Date});
 const now=Date.now()/1000,start=now-300,end=start+900;
 const points=Array.from({length:100},(_,i)=>[start+i*3,81040-i*.27+Math.sin(i/4)*3]);
 const d={status:'running',market:'btc-updown-15m-example',updated:new Date().toISOString(),reference_price:{status:'fresh',window_start:start,window_end:end},brti_chart:{market:'btc-updown-15m-example',points},target_behavior:{target:81041},settlement_forecast:{status:'available',target_usd:81041,forecast_final_mean_usd:81001,seconds_to_settlement:600}};
 const model=roundWindow.btcRoundChartModel;
 assert.equal(model(d,now).forecast,81001);
 assert.equal(model({...d,updated:new Date((now-60)*1000).toISOString()},now).forecast,null);
 assert.equal(model(d,end),null);
 assert.equal(model({...d,brti_chart:{...d.brti_chart,market:'old'}},now),null);
 assert.equal(model({...d,target_behavior:{target:null}},now).forecast,null);
 assert.equal(model({...d,settlement_forecast:{...d.settlement_forecast,target_usd:80000}},now).forecast,null);
 assert.equal(model({...d,reference_price:{...d.reference_price,status:'stale'}},now).forecast,null);
 roundWindow.btcServerStatus=d;
 assert.equal(roundWindow.drawBtcRoundChart(canvas,{bars:[],ema:true,candles:true}),true);
 assert.match(note.textContent,/illustrative guide/);
 assert.ok(drawing.length>100);
 assert.ok(drawing.every(call=>call.every(v=>typeof v!=='number'||Number.isFinite(v))));
 assert.ok(drawing.some(call=>call[0]==='fillText'&&call[1]==='Final 60s'));
 console.log('PASS: round renderer at 390px; stale/target/market/rollover guards; finite canvas geometry');
}
