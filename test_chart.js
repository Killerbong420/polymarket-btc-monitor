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
 console.log('PASS: all inline JS parses; chart render, forecast, feed failure, resume/recovery');
})().catch(e=>{console.error(e);process.exitCode=1});
