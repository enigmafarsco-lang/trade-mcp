/* Optional browser check against a mock console only. Same setup as browser_smoke.cjs. */
const assert=require('node:assert/strict');
const {chromium}=require('playwright-core');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage'],headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1400,height:1000}});let modelCalls=0,agentCalls=0,confirmCalls=0,budgetCalls=0;
  const message='اعتبار سرویس مدل کافی نیست؛ ایجنت متوقف است و پیشنهادی ساخته نشد.';
  const state={backend:'mock',trading_enabled:false,model_backend:'openai-compatible',model_source:'web',
   model_configured:true,model:'web-model',model_provider:'provider.example',
   model_billing_project:'web-project',model_usage_convention:'openai_inclusive',
   model_budget:{allowed:false,state:'not_activated',max_attempts:5,window_seconds:300,used_attempts:0,first_attempt_at:null},
   model_usage:{requests:0,ok_rows:0,failed_rows:0,cost_unknown_rows:0,usage_unknown_rows:0,usage_partial_rows:0,
    fields:{},cost:[],accounting_conventions:{}},model_usage_rows:[],run_history:[],scheduler:{running:false},
   model_test:{state:'not_tested',message:'پاسخ مدل هنوز آزمایش نشده'},
   agents:[{id:'test-agent',name:'<img src=x onerror=alert(1)>',symbols:['XAUUSD'],last_run:1700000000,
    report:{used_llm:false,proposals:[],decisions:[{symbol:'XAUUSD',action:'wait',source:'opencode_error',model_error:{code:'billing_required',message}}]}}],
   proposals:[{id:'old-test',created_at:1700000000,status:'expired',confirmable:false,payload:{symbol:'XAUUSD',volume:.01}}]};
  const reply=(route,data)=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  await page.route('**/api/workspace-status',r=>reply(r,state));
  await page.route('**/api/model-budget',r=>{budgetCalls++;assert.equal(r.request().postDataJSON().action,'activate');
   state.model_budget={allowed:true,state:'active',max_attempts:5,window_seconds:300,used_attempts:0,first_attempt_at:null};
   return reply(r,{ok:true,budget:state.model_budget});});
  await page.route('**/api/model-test',r=>{modelCalls++;state.model_test={state:'failed',code:'billing_required',message,tested_at:1700000000};return reply(r,{ok:false,error:'billing_required',message});});
  await page.route('**/api/agents/test-agent',r=>{agentCalls++;assert.equal(r.request().postDataJSON().action,'run_advisory');return reply(r,{ok:true,report:state.agents[0].report});});
  await page.route('**/api/proposals/*/confirm',r=>{confirmCalls++;return reply(r,{ok:false,error:'test_must_not_confirm'});});
  await page.goto(process.env.CONSOLE_URL||'http://127.0.0.1:8794');
  await page.locator('#workspace-agents').getByText('opencode_error',{exact:false}).waitFor();
  assert.ok((await page.locator('#workspace-agents').textContent()).includes('billing_required'));
  assert.equal(await page.locator('#workspace-agents img').count(),0,'agent names are text, not HTML');
  await page.waitForFunction(()=>document.querySelector('#workspace-budget').textContent.includes('فعال نیست'));
  assert.equal(await page.locator('#workspace-test').isDisabled(),true,'budget must be activated first');
  await page.waitForTimeout(5300);
  assert.equal(modelCalls,0,'saved-report polling must not call the model');
  assert.equal(agentCalls,0,'saved-report polling must not run agents');
  await page.click('#workspace-budget-activate');
  await page.waitForFunction(()=>!document.querySelector('#workspace-test').disabled);
  assert.equal(budgetCalls,1);
  await page.click('#workspace-test');
  await page.waitForFunction(()=>document.querySelector('#workspace-model').textContent.includes('billing_required'));
  assert.equal(modelCalls,1);
  await page.locator('#workspace-agents button').click();
  await page.waitForFunction(()=>document.querySelector('#workspace-action').textContent.includes('گزارش ذخیره شد'));
  assert.equal(agentCalls,1);
  assert.equal(await page.locator('#workspace-history button').count(),0,'expired proposal has no confirmation control');
  assert.equal(confirmCalls,0);
  const chartBox=await page.locator('#desk-charts').boundingBox(),panelBox=await page.locator('#workspace-panel').boundingBox();
  assert.ok(chartBox.y<panelBox.y,'charts must be above analysis');
  let startCalls=0,stopCalls=0;
  state.model_test={state:'ready',tested_at:1700000000,message:'آزمایش موفق'};
  state.page_cycle={active:false,busy:false,reason:'Stopped'};
  await page.click('#workspace-refresh');
  await page.waitForFunction(()=>!document.querySelector('#workspace-cycle-start').disabled);
  await page.route('**/api/page-cycle',r=>{const p=r.request().postDataJSON();
   if(p.action==='start'){assert.equal(p.consent_model_requests,true);startCalls++;state.page_cycle.active=true;return reply(r,{ok:true,lease:'page-test-lease'});}
   assert.equal(p.lease,'page-test-lease');if(p.action==='stop'){stopCalls++;state.page_cycle.active=false;}return reply(r,{ok:true});
  });
  await page.check('#workspace-cycle-consent');await page.click('#workspace-cycle-start');
  await page.waitForFunction(()=>document.querySelector('#workspace-cycle-state').textContent.includes('چرخه فعال'));
  const stopped=page.waitForResponse(r=>r.url().includes('/api/page-cycle')&&r.request().postDataJSON().action==='stop');
  await page.evaluate(()=>window.dispatchEvent(new Event('pagehide')));await stopped;
  assert.equal(startCalls,1);assert.equal(stopCalls,1);
  assert.equal(confirmCalls,0);
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.locator('#workspace-panel').isVisible());
  console.log('Workspace browser check passed: budget gate, saved reports, billing visibility, no automatic model/agent/confirmation calls, expiry, safe text, mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1);});
