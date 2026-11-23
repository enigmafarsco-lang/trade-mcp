/* Synthetic UI smoke test against the local mock console. No real browser or broker. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
function element(id){
 if(!elements.has(id)){
  let value='';
  elements.set(id,{get value(){return value;},set value(v){value=String(v);},
   checked:false,disabled:false,textContent:'',dataset:{},listeners:{},
   addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);},
   children:[],append(){},replaceChildren(){},querySelectorAll(){return [];}});
 }
 return elements.get(id);
}
const documentEvents={},windowEvents={};
global.document={getElementById:element,addEventListener(type,fn){(documentEvents[type] ||= []).push(fn);},createElement(){return element(Symbol());},hidden:false};
global.window={addEventListener(type,fn){(windowEvents[type] ||= []).push(fn);}};
global.setInterval=()=>0;
const realFetch=global.fetch;
const requests=[];
let delayPlan=false,releasePlan=null,delayQuote=false,notifyQuoteStarted=null;
global.fetch=(path,options)=>{
 requests.push(path);
 if(path==='/api/daytrade/plan'&&delayPlan){
  delayPlan=false;return new Promise((resolve,reject)=>{releasePlan=()=>realFetch(process.env.TEST_CONSOLE_URL+path,options).then(resolve,reject);});
 }
 if(path.startsWith('/api/daytrade/'))return realFetch(process.env.TEST_CONSOLE_URL+path,options);
 if(path.startsWith('/api/quotes?')&&delayQuote){
  if(notifyQuoteStarted)notifyQuoteStarted();
  return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true}));
 }
 if(path.startsWith('/api/quotes?'))return Promise.resolve(new Response(JSON.stringify({ok:true,quotes:{XAUUSD_i:{bid:2600,ask:2600.2}}}),{status:200,headers:{'Content-Type':'application/json'}}));
 return Promise.reject(new Error('non-workbench request deliberately unavailable'));
};
vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8'),{filename:'workspace.js'});
(async()=>{
 element('daytrade-sample').onclick();
 const sample=JSON.parse(element('daytrade-sessions').value);
 assert.equal(sample.length,20);
 const current=element('daytrade-bars').value;
 element('daytrade-bars').value=JSON.stringify([...sample.flat(),JSON.parse(current)[0]]);
 element('daytrade-split').onclick();
 assert.equal(JSON.parse(element('daytrade-sessions').value).length,20);
 const split=element('daytrade-sessions').value;
 element('daytrade-bars').value='[]';element('daytrade-split').onclick();
 assert.equal(element('daytrade-sessions').value,split);
 element('daytrade-bars').value=current;
 await element('daytrade-entry-run').onclick();
 const signal=JSON.parse(element('daytrade-entry-result').textContent);
 assert.equal(signal.status,'triggered');assert.equal(signal.trigger.slot,5);
 await element('daytrade-plan-run').onclick();
 const plan=JSON.parse(element('daytrade-plan-result').textContent);
 assert.equal(plan.ok,true);assert.equal(plan.execution_authorized,false);
 element('daytrade-target-price').value='2704';
 await element('daytrade-plan-run').onclick();
 assert.equal(JSON.parse(element('daytrade-plan-result').textContent).target,2704);
 await element('daytrade-history-run').onclick();
 assert(element('daytrade-sim-result').textContent.includes('هدف ثابت'));
 element('daytrade-target-price').value='';
 await element('daytrade-history-run').onclick();
 const history=JSON.parse(element('daytrade-sim-result').textContent);
 assert.equal(history.historical_segments,20);assert.equal(history.forced_exit_at_horizon,false);
 await element('daytrade-machine-run').onclick();
 const machine=JSON.parse(element('daytrade-sim-result').textContent);
 assert.equal(machine.replays,1000);assert.equal(machine.forced_exit_at_horizon,false);
 await element('daytrade-alert-run').onclick();
 const alert=JSON.parse(element('daytrade-alert-result').textContent);
 assert.equal(alert.automatic_monitoring,false);
 assert(requests.filter(path=>path.startsWith('/api/daytrade/')).length===8);
 assert(requests.every(path=>path==='/api/workspace-status'||path.startsWith('/api/daytrade/')));
 // All extra examples use the mock console and caller-supplied synthetic data.
 for(const kind of ['scan','alert-rules','review','news','stock-facts','stock-disclosures']){
  element('daytrade-extra-kind').value=kind;element('daytrade-extra-example').onclick();
  await element('daytrade-extra-run').onclick();
  assert.equal(JSON.parse(element('daytrade-extra-result').textContent).ok,true,kind);
 }
 // No price is requested until the explicit start. One synthetic quote hits the stop.
 assert(!requests.some(path=>path.startsWith('/api/quotes?')));
 element('daytrade-monitor-symbol').value='XAUUSD_i';
 await element('daytrade-monitor-start').onclick();
 assert(requests.some(path=>path.startsWith('/api/quotes?')));
 assert(element('daytrade-monitor-status').textContent.includes('پایش فعال'));
 assert(element('daytrade-monitor-events').textContent.includes('stop'));
 assert.equal(element('daytrade-monitor-stop').disabled,false);
 // An input change stops the session; a previously latched rule cannot keep running.
 for(const cb of element('daytrade-stop-value').listeners.input)cb();
 assert(element('daytrade-monitor-status').textContent.includes('متوقف شد'));
 assert.equal(element('daytrade-monitor-stop').disabled,true);
 // Stop during plan preparation must not start a late quote poll.
 let quoteCount=requests.filter(path=>path.startsWith('/api/quotes?')).length;
 delayPlan=true;const preparing=element('daytrade-monitor-start').onclick();
 element('daytrade-monitor-stop').onclick();releasePlan();await preparing;
 assert.equal(requests.filter(path=>path.startsWith('/api/quotes?')).length,quoteCount);
 assert.equal(element('daytrade-monitor-stop').disabled,true);
 // Stop during an in-flight quote aborts it, with no later alert evaluation.
 let alertCount=requests.filter(path=>path==='/api/daytrade/alert-rules').length;
 delayQuote=true;const started=new Promise(resolve=>{notifyQuoteStarted=resolve;});
 const polling=element('daytrade-monitor-start').onclick();await started;
 element('daytrade-monitor-stop').onclick();await polling;delayQuote=false;
 assert.equal(requests.filter(path=>path==='/api/daytrade/alert-rules').length,alertCount);
 assert.equal(element('daytrade-monitor-stop').disabled,true);
 // Hiding the document while a quote is in flight also cancels the monitor.
 delayQuote=true;const hiddenStart=new Promise(resolve=>{notifyQuoteStarted=resolve;});
 const hiddenPoll=element('daytrade-monitor-start').onclick();await hiddenStart;
 document.hidden=true;for(const callback of documentEvents.visibilitychange)callback();await hiddenPoll;
 assert.equal(element('daytrade-monitor-stop').disabled,true);
 assert(element('daytrade-monitor-status').textContent.includes('پنهان'));
 assert.equal(requests.filter(path=>path==='/api/daytrade/alert-rules').length,alertCount);
 document.hidden=false;delayQuote=false;
 console.log('synthetic UI workflow passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
