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
   checked:false,disabled:false,textContent:'',dataset:{},
   children:[],append(){},replaceChildren(){},querySelectorAll(){return [];}});
 }
 return elements.get(id);
}
global.document={getElementById:element,addEventListener(){},createElement(){return element(Symbol());},hidden:false};
global.window={addEventListener(){}};
global.setInterval=()=>0;
const realFetch=global.fetch;
const requests=[];
global.fetch=(path,options)=>{
 requests.push(path);
 if(path.startsWith('/api/daytrade/'))return realFetch(process.env.TEST_CONSOLE_URL+path,options);
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
 console.log('synthetic UI workflow passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
