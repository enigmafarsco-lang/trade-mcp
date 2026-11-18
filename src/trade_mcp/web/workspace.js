/* Visible agent activity. Polling reads saved results only; model calls are explicit. */
(() => {
'use strict';
const node=id=>document.getElementById(id);
const make=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const busy=new Set();let refreshing=false,lease=null,modelLoaded=false,testing=false;
let budgetAllowed=false,modelReady=false,connectionKnown=false;
const time=v=>v?new Date(v*1000).toLocaleString('fa-IR'):'هنوز اجرا نشده';
const duration=ms=>{const mins=Math.ceil(ms/60000);return mins>=60?`${Math.floor(mins/60).toLocaleString('fa-IR')} ساعت و ${(mins%60).toLocaleString('fa-IR')} دقیقه`:`${mins.toLocaleString('fa-IR')} دقیقه`;};
const known=(v)=>v===null||v===undefined?'نامشخص':Number(v).toLocaleString('fa-IR');
function syncTestButton(){
 const b=node('workspace-test');if(!b)return;
 b.disabled=testing||!connectionKnown||!budgetAllowed||!modelReady;
 b.textContent=!connectionKnown?'وضعیت اتصال نامشخص — ابتدا بازخوانی کنید':testing?'در حال آزمایش مدل…':!modelReady?'مدل وب تنظیم نشده':!budgetAllowed?'آزمایش مدل — ابتدا بودجه را فعال کنید':'آزمایش پاسخ مدل (ممکن است هزینه داشته باشد)';
}
function budgetText(b){
 b=b||{};
 const state=b.state||'not_activated';
 if(state==='active'&&b.reason==='provider_cooldown'){
  const left=Number(b.retry_after_ms||0);
  return `بودجه فعال است، اما provider تا ${duration(left)} دیگر درخواست جدید نمی‌پذیرد. تلاش خودکار انجام نمی‌شود.`;
 }
 if(state==='active'){
  if(b.first_attempt_at==null)return `فعال و هنوز شروع نشده: حداکثر ${b.max_attempts||5} تلاش در پنجرهٔ ${b.window_seconds||300} ثانیه‌ای؛ پنجره با نخستین درخواست مجاز آغاز می‌شود.`;
  const secs=Number(b.seconds_remaining||0);
  return `فعال: ${b.used_attempts||0} از ${b.max_attempts||5} تلاش مصرف شده؛ ${Math.ceil(secs)} ثانیه از پنجره باقی است.`;
 }
 if(state==='exhausted')return 'پایان‌یافته: سقف پنج تلاش مصرف شده است. پنجرهٔ جدید فقط با فعال‌سازی صریح شروع می‌شود.';
 if(state==='expired')return 'پایان‌یافته: پنجرهٔ ۳۰۰ ثانیه‌ای تمام شده است. پنجرهٔ جدید فقط با فعال‌سازی صریح شروع می‌شود.';
 if(state==='revoked')return 'لغو شده است. برای ادامه باید پنجرهٔ جدید را صریحاً فعال کنید.';
 return 'فعال نیست؛ هیچ درخواست مدلی در مسیر وب مجاز نیست.';
}
function syncBudgetButton(b){
 b=b||{};
 const activate=node('workspace-budget-activate'),revoke=node('workspace-budget-revoke');
 if(activate){
  activate.disabled=b.state==='active';
  activate.textContent=b.state==='active'?'بودجهٔ فعال است (فعال‌سازی دوباره پنجره را بازنشانی نمی‌کند)':b.state==='not_activated'?'فعال‌سازی صریح بودجهٔ آزمایش':'شروع صریح پنجرهٔ جدید (۳۰۰ ثانیه / ۵ تلاش)';
 }
 if(revoke)revoke.disabled=b.state!=='active';
}
function usageText(u){
 u=u||{};const f=u.fields||{};
 const field=name=>{const x=f[name]||{};return `${known(x.known_total)} مجموع معلوم (${x.known_rows||0} ردیف) | نامشخص: ${x.unknown_rows||0} ردیف`;};
 const conventions=Object.keys(u.accounting_conventions||{}).filter(Boolean);
 const convention=conventions.length?conventions.join('، '):'نامشخص';
 const costs=(u.cost||[]).length?(u.cost||[]).map(c=>`${known(c.known_total)} ${c.currency||'واحد پول نامشخص'} (${c.known_rows||0} ردیف)`).join(' | '):'نامشخص';
 return [
  `درخواست‌های واقعی ثبت‌شده: ${u.requests||0} | موفق: ${u.ok_rows||0} | ناموفق: ${u.failed_rows||0}`,
  `input: ${field('input_tokens')}`,
  `cached input: ${field('cached_input_tokens')}`,
  `output: ${field('output_tokens')}`,
  `reasoning: ${field('reasoning_tokens')}`,
  `هزینهٔ گزارش‌شدهٔ provider: ${costs} | ردیف‌های بدون هزینه: ${u.cost_unknown_rows||0}`,
  `قرارداد usage: ${convention} — cached input ممکن است داخل input و reasoning داخل output باشد؛ این صفحه جمع دوباره نمی‌سازد.`,
  'این دفتر فقط تلاش‌های provider اختصاصی وب را نشان می‌دهد و با آمار OpenCode جمع نمی‌شود. مقدار نامشخص صفر نیست.'
 ].join('\n');
}
function usageRows(rows){
 const box=node('workspace-usage-rows');if(!box)return;
 box.replaceChildren(make('h3','آخرین تلاش‌های provider وب'));
 if(!(rows||[]).length){box.append(make('p','هنوز تلاش واقعی ثبت نشده است.'));return;}
 const table=make('table');
 const head=make('tr');
 for(const title of ['زمان','نوع','مدل','وضعیت','input','cached','output','reasoning','هزینه'])head.append(make('th',title));
 table.append(head);
 for(const row of rows){
  const tr=make('tr');
  const cost=row.cost_amount==null?'نامشخص':`${known(row.cost_amount)} ${row.cost_currency||'واحد نامشخص'}`;
  for(const value of [
   time(row.created_at),row.request_kind||'—',row.model||'—',
   row.ok?'موفق':(row.error_code||'ناموفق'),
   known(row.input_tokens),known(row.cached_input_tokens),known(row.output_tokens),
   known(row.reasoning_tokens),cost
  ])tr.append(make('td',value));
  table.append(tr);
 }
 box.append(table);
}
async function request(path,body){
 const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 let data=null;try{data=await r.json();}catch(_){}
 if(!r.ok){const err=new Error((data&&(data.message||data.error))||('HTTP '+r.status));err.payload=data;throw err;}
 return data||{};
}
function describe(report){
 const lines=[`مدل واقعاً استفاده شد: ${report.used_llm?'بله':'خیر'} | پیشنهادهای این اجرا: ${(report.proposals||[]).length}`];
 for(const d of report.decisions||[]){
  lines.push(`${d.symbol}: ${{buy:'خرید',sell:'فروش',wait:'انتظار'}[d.action]||'نامشخص'} | منبع: ${d.source||'—'}`);
  if(d.response_model)lines.push('مدل پاسخ‌دهنده: '+d.response_model+' / '+(d.response_variant||'default'));
  for(const a of d.model_attempts||[])lines.push('تلاش مدل: '+a.model+' / '+(a.variant||'default')+' • '+a.status+(a.error?' • '+a.error:''));
  if(d.model_error)lines.push(`توقف مدل: ${d.model_error.message} (${d.model_error.code})`);
  if(d.reason)lines.push(d.reason);
  if(d.note)lines.push(d.note);
  const technical=d.technical_analysis||{},scenario=technical.scenario||{};
  if(technical.summary)lines.push('تحلیل فنی قاعده‌محور — نه تصمیم مدل و نه سفارش:\n'+technical.summary);
  for(const warning of technical.warnings||[])lines.push('هشدار بازار/برنامه: '+warning);
  if(scenario.sizing){const z=scenario.sizing;lines.push(`بودجهٔ مبنای حجم: ${z.effective_sizing_balance??'—'} | بودجهٔ زیان: ${z.risk_amount??'—'} | لات محاسبه‌شده: ${z.volume??'—'}`);if(z.error)lines.push(z.error);}
  const p=d.preview||{};
  if(Object.keys(p).length)lines.push(`ورود مرجع: ${p.estimated_entry??'—'} | لات: ${p.volume??'—'} | SL: ${p.stop_loss??'—'} | TP: ${p.take_profit??'—'}\nزیان تخمینی: ${p.risk_amount??'—'} | سود هدف پیش از هزینه: ${p.estimated_profit??'—'} | مارجین: ${p.estimated_margin??'—'}`);
  if(d.proposal_id)lines.push(`شناسهٔ پیشنهاد: ${d.proposal_id} | وضعیت: ${d.status||'—'}`);
 }
 for(const e of report.errors||[])lines.push(`خطا: ${e.symbol||''} ${e.error||''}`);
 return lines.join('\n');
}
async function refresh(){
 if(refreshing)return;refreshing=true;
 try{
  const d=await request('/api/workspace-status');
  connectionKnown=true;
  budgetAllowed=!!(d.model_budget&&d.model_budget.allowed);
  modelReady=!!d.model_configured;
  node('workspace-status').textContent=`داده: ${d.backend}${d.backend==='mock'?' — ساختگی؛ دادهٔ واقعی نیست':''} | اجرای معامله: ${d.trading_enabled?'فعال؛ هر پیشنهاد نیازمند تأیید شماست':'غیرفعال'} | provider: ${d.model_provider||'—'} | مدل: ${d.model||'تنظیم نشده'}\n${d.scheduler.running?'چرخهٔ ایجنت فعال است':'چرخهٔ خودکار خاموش است؛ دریافت قیمت به معنی معامله نیست.'}`;
  node('workspace-provider').textContent=`backend: ${d.model_backend||'—'} | provider: ${d.model_provider||'نامشخص'} | مدل: ${d.model||'تنظیم نشده'} | وضعیت: ${d.model_configured?'تنظیم شده؛ فقط مسیر صریح انتخاب‌شده استفاده می‌شود':'تنظیم نشده؛ درخواست مدل مسدود است'}\nبرچسب billing: ${d.model_billing_project||'ندارد'} (فقط metadata؛ اثبات صورت‌حساب جدا نیست)\nقرارداد usage: ${d.model_usage_convention||'unknown'}`;
  node('workspace-budget').textContent=budgetText(d.model_budget);
  syncBudgetButton(d.model_budget);
  node('workspace-usage').textContent=usageText(d.model_usage);
  usageRows(d.model_usage_rows||[]);
  node('research-analyze').disabled=false;
  const cycle=d.page_cycle||{};
  node('workspace-cycle-state').textContent=`${cycle.active?'چرخه فعال':'چرخه متوقف'} | ${cycle.busy?'در حال پایان تحلیل':''} ${cycle.reason||''}`;
  if(!cycle.active)lease=null;
  const test=d.model_test||{};
  const testReady=test.state==='ready';
  node('workspace-cycle-start').disabled=!!cycle.active||!!cycle.busy||!budgetAllowed||!modelReady||!testReady;
  node('workspace-cycle-stop').disabled=!lease;
  if(!modelLoaded){node('workspace-model-id').value=d.model||'';modelLoaded=true;}
  const select=node('workspace-cycle-agent'),chosen=select.value;
  const ids=(d.agents||[]).map(a=>a.id).join(',');
  if(select.dataset.ids!==ids){select.replaceChildren();for(const a of d.agents||[]){const option=make('option',a.name+' • '+a.symbols.join(', '));option.value=a.id;select.append(option);}select.value=chosen||d.agents[0]?.id||'';select.dataset.ids=ids;}
  const testLines=[`آخرین آزمایش پاسخ مدل: ${test.state==='not_tested'?'هنوز انجام نشده':`${test.state==='failed'?'ناموفق':test.state==='ready'?'موفق':test.state} — ${time(test.tested_at)}`}`];
  if(test.message)testLines.push(test.message);
  if(test.code)testLines.push('کد: '+test.code);
  if(test.response_model)testLines.push('مدل پاسخ‌دهندهٔ تست: '+test.response_model);
  for(const a of test.model_attempts||[])testLines.push('تلاش: '+a.model+' • '+(a.status||'—')+(a.error?' • '+a.error:''));
  if(test.retry_after_ms)testLines.push('provider تا حدود '+duration(Number(test.retry_after_ms))+' دیگر درخواست جدید نمی‌پذیرد.');
  node('workspace-model').textContent=testLines.join('\n');
  syncTestButton();
  const box=node('workspace-agents');box.replaceChildren();
  for(const a of d.agents||[]){
   const card=make('div');card.className='card';
   card.append(make('h3',a.name+' • '+a.symbols.join(', ')),make('p','آخرین اجرا: '+time(a.last_run)));
   const run=make('button',busy.has(a.id)?'در حال تحلیل…':budgetAllowed?'تحلیل با ایجنت — بدون اجرای معامله':'تحلیل با ایجنت — نیازمند بودجهٔ فعال');run.className='primary';run.disabled=busy.has(a.id)||!budgetAllowed||!modelReady;run.title=!budgetAllowed?'ابتدا بودجهٔ آزمایش را فعال کنید.':'';
   run.onclick=async()=>{if(busy.has(a.id))return;busy.add(a.id);run.disabled=true;run.textContent='در حال تحلیل…';node('workspace-action').textContent='در انتظار نتیجهٔ ایجنت؛ هیچ تأییدی ارسال نمی‌شود.';
    try{const result=await request('/api/agents/'+encodeURIComponent(a.id),{action:'run_advisory'});node('workspace-action').textContent=result.ok?'گزارش ذخیره شد؛ نتیجه در کارت ایجنت نمایش داده می‌شود.':result.message||result.error||'اجرای ناموفق';}
    catch(e){node('workspace-action').textContent=(e&&e.message)||'دریافت نتیجه ناموفق بود؛ قبل از تکرار، آخرین گزارش را بررسی کنید.';}
    finally{busy.delete(a.id);await refresh();}
   };
   card.append(run,make('pre',a.last_run?describe(a.report||{}):'هنوز گزارشی وجود ندارد. روشن‌بودن ایجنت به‌تنهایی به معنی اجرای آن نیست.'));box.append(card);
  }
  if(!d.agents.length)box.append(make('p','ایجنتی تعریف نشده؛ در تب ایجنت‌ها یک ایجنت مشورتی بسازید.'));
  const proposals=node('workspace-proposals'),history=node('workspace-history');proposals.replaceChildren();history.replaceChildren();
  proposals.append(make('h3','پیشنهادها و سابقه — سفارش جدید خودکار اجرا نمی‌شود'));
  for(const p of d.proposals||[]){
   const payload=p.payload||{},card=make('details');
   card.append(make('summary',`${p.id} | ${payload.symbol||'—'} | ${p.status==='expired'?'منقضی؛ قابل تأیید نیست':p.status} | ${time(p.created_at)}${p.status==='pending'?' | اعتبار: '+Math.max(0,Math.ceil(p.expires_at-Date.now()/1000))+' ثانیه':''}`));
   card.append(make('pre',JSON.stringify(payload,null,2)));
   const actionable=!!p.confirmable&&(p.status==='pending'||p.status==='blocked');
   if(actionable){
    const confirm=make('button','تأیید و اجرای همین پیشنهاد');confirm.className='primary';
    confirm.onclick=async()=>{
     if(!window.confirm(`اجرای واقعی همین پیشنهاد روی حساب متصل؟\n${p.id}\n${JSON.stringify(payload,null,2)}`))return;
     confirm.disabled=true;confirm.textContent='در حال ارسال پیشنهاد…';
     node('workspace-action').textContent='در حال ارسال پیشنهاد برای تأیید؛ تا دریافت نتیجه دوباره کلیک نکنید.';
     try{const result=await request('/api/proposals/'+encodeURIComponent(p.id)+'/confirm',{});node('workspace-action').textContent=result.ok?'هسته اجرای پیشنهاد را پذیرفت؛ نتیجه و پوزیشن‌ها را بررسی کنید.':(result.message||result.error||'اجرا نشد');}
     catch(e){node('workspace-action').textContent=(e&&e.message)||'نتیجهٔ اجرا نامشخص است؛ پیش از تکرار، پوزیشن‌ها را بررسی کنید.';}
     finally{await refresh();}
    };
    card.append(confirm);
   }else if(p.status==='pending'||p.status==='blocked'){
    card.append(make('p','این پیشنهاد در وضعیت فعلی قابل تأیید نیست؛ کنترل اجرا نمایش داده نمی‌شود.'));
   }
   (p.status==='pending'||p.status==='blocked'?proposals:history).append(card);
  }
  if(!d.proposals.some(p=>p.status==='pending'||p.status==='blocked'))proposals.append(make('p','پیشنهاد فعال جدیدی وجود ندارد. موارد قدیمی در تاریخچه‌اند.'));
  const runs=node('workspace-run-history');runs.replaceChildren(make('h3','تاریخچهٔ اجراهای ایجنت (اجراهای ثبت‌شده از این نسخه)'));
  for(const item of d.run_history||[]){const detail=make('details');detail.append(make('summary','گزارش تاریخی • '+time(item.created_at)+' • '+item.agent_id),make('pre','وضعیت زیر مربوط به زمان همین اجراست؛ وضعیت فعلی در فهرست پیشنهادهاست.\n'+describe(item.report||{})));runs.append(detail);}
 }catch(_){connectionKnown=false;budgetAllowed=false;modelReady=false;syncTestButton();
  node('workspace-cycle-start').disabled=true;node('research-analyze').disabled=true;
  for(const b of node('workspace-agents').querySelectorAll('button'))b.disabled=true;
  node('workspace-status').textContent='ارتباط با کنسول برقرار نشد؛ وضعیت مدل و بودجه نامشخص است، نه الزاماً تنظیم‌نشده. داده‌های زیر مربوط به آخرین دریافت موفق‌اند.';
  node('workspace-provider').textContent='وضعیت زندهٔ اتصال مدل در دسترس نیست؛ دادهٔ قبلی قابل اتکا نیست.';}
 finally{refreshing=false;}
}
async function stopCycle(beacon=false){
 if(!lease)return;const token=lease;lease=null;
 const body={action:'stop',lease:token};
 if(beacon){navigator.sendBeacon('/api/page-cycle',new Blob([JSON.stringify(body)],{type:'application/json'}));return;}
 try{await request('/api/page-cycle',body);}
 catch(e){node('workspace-action').textContent=(e&&e.message)||'توقف چرخه ناموفق بود؛ وضعیت را دوباره بررسی کنید.';}
 finally{await refresh();}
}
node('workspace-connect-opencode').onclick=async()=>{
 const button=node('workspace-connect-opencode');button.disabled=true;
 node('workspace-action').textContent='در حال شناسایی امن OpenCode؛ درخواست تولید متن ارسال نمی‌شود…';
 try{
  const model=node('workspace-model-id').value.trim();
  if(!model)throw new Error('شناسهٔ مدل رایگان را وارد کنید؛ مثلاً opencode/mimo-v2.6-flash-free');
  const d=await request('/api/model-connect-opencode',{model,consent_connect:true});
  node('workspace-action').textContent=d.ok?'اتصال سرور تأیید و مدل وب تنظیم شد؛ هنوز پاسخ هوش مصنوعی آزمایش نشده است.':(d.message||d.error);
 }catch(e){node('workspace-action').textContent=e.message||'اتصال ناموفق بود.';}
 finally{button.disabled=false;await refresh();}
};
node('workspace-save-model').onclick=async()=>{
 const b=node('workspace-save-model');b.disabled=true;
 node('workspace-action').textContent='در حال ذخیرهٔ تنظیمات مدل در همین اجرای وب…';
 try{
  const d=await request('/api/model-config',{model:node('workspace-model-id').value});
  node('workspace-action').textContent=d.ok?'مدل وب اعمال شد؛ تنظیم چت OpenCode عوض نشد. اکنون پاسخ مدل را آزمایش کنید.':(d.message||d.error||'تنظیمات مدل اعمال نشد.');
 }catch(e){node('workspace-action').textContent=(e&&e.message)||'اعمال تنظیمات مدل ناموفق بود.';}
 finally{b.disabled=false;await refresh();}
};
node('workspace-budget-activate').onclick=async()=>{
 const b=node('workspace-budget-activate');b.disabled=true;
 node('workspace-action').textContent='در حال فعال‌سازی صریح بودجهٔ آزمایش…';
 try{
  const d=await request('/api/model-budget',{action:'activate'});
  node('workspace-action').textContent=d.ok?'بودجهٔ آزمایش فعال شد؛ پنجره با نخستین درخواست آغاز می‌شود.':(d.message||d.error||'فعال‌سازی ناموفق بود.');
 }catch(e){node('workspace-action').textContent=(e&&e.message)||'فعال‌سازی بودجه ناموفق بود.';}
 finally{await refresh();}
};
node('workspace-budget-revoke').onclick=async()=>{
 const b=node('workspace-budget-revoke');b.disabled=true;
 try{await request('/api/model-budget',{action:'revoke'});node('workspace-action').textContent='بودجهٔ آزمایش لغو شد؛ برای ادامه فعال‌سازی صریح لازم است.';}
 catch(e){node('workspace-action').textContent=(e&&e.message)||'لغو بودجه ناموفق بود.';}
 finally{await refresh();}
};
node('workspace-cycle-start').onclick=async()=>{
 if(document.hidden)return;
 const b=node('workspace-cycle-start');b.disabled=true;
 node('workspace-action').textContent='در حال شروع چرخهٔ مشورتی…';
 try{
  const d=await request('/api/page-cycle',{action:'start',agent_id:node('workspace-cycle-agent').value,
   interval:Number(node('workspace-cycle-interval').value),consent_model_requests:node('workspace-cycle-consent').checked});
  if(d.ok)lease=d.lease;
  node('workspace-action').textContent=d.ok?'چرخهٔ مشورتی شروع شد؛ اجرای معامله فقط با تأیید جداگانه است.':(d.message||d.error||'چرخه شروع نشد.');
 }catch(e){node('workspace-action').textContent=(e&&e.message)||'شروع چرخه ناموفق بود.';}
 finally{await refresh();}
};
node('workspace-cycle-stop').onclick=()=>stopCycle();
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopCycle(true);});
window.addEventListener('pagehide',()=>stopCycle(true));
setInterval(async()=>{if(lease&&!document.hidden){try{const d=await request('/api/page-cycle',{action:'heartbeat',lease});if(!d.ok)lease=null;}catch(_){node('workspace-action').textContent='heartbeat ناموفق؛ چرخه بدون heartbeat معتبر متوقف خواهد شد.';}}},5000);
node('workspace-refresh').onclick=refresh;
let researchReport=null;
node('research-download').onclick=()=>{
 if(!researchReport)return;
 const url=URL.createObjectURL(new Blob([JSON.stringify(researchReport,null,2)],{type:'application/json'}));
 const a=document.createElement('a');a.href=url;a.download='research-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';
 a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
};
for(const operation of ['analyze','backtest','compare'])node('research-'+operation).onclick=async()=>{
 const out=node('research-result');
 if(operation==='analyze'&&!node('research-consent').checked){out.textContent='ابتدا اجازهٔ درخواست مدل را مشخص کنید.';return;}
 for(const op of ['analyze','backtest','compare'])node('research-'+op).disabled=true;
 out.textContent='در حال پژوهش؛ هیچ سفارشی اجرا نمی‌شود…';
 try{
  const d=await request('/api/research/'+operation,{role:node('research-role').value,strategy:node('research-strategy').value,agent_id:node('workspace-cycle-agent').value,consent_model_request:node('research-consent').checked});
  if(operation!=='analyze'){researchReport=d;node('research-download').disabled=false;}
  const display={...d};if(display.snapshot){display.snapshot={...display.snapshot};delete display.snapshot.bars;}
  out.textContent=d.ok?(d.report||JSON.stringify(display,null,2)):(d.message||d.error||'پژوهش ناموفق بود.');
 }catch(e){
  if(operation!=='analyze'&&e.payload){researchReport=e.payload;node('research-download').disabled=false;}
  out.textContent=e.message||'پژوهش ناموفق بود.';
 }
 finally{for(const op of ['analyze','backtest','compare'])node('research-'+op).disabled=false;await refresh();}
};
node('workspace-estimate').onclick=async()=>{
 const button=node('workspace-estimate'),result=node('workspace-estimate-result');
 button.disabled=true;result.textContent='در حال ساخت نمونهٔ تحلیل؛ بدون درخواست مدل…';
 try{
  const d=await request('/api/estimate-analysis-tokens',{agent_id:node('workspace-cycle-agent').value});
  if(!d.ok)throw new Error(d.message||d.error||'تخمین ناموفق بود.');
  result.textContent=`منبع: ${d.backend}${d.backend==='mock'?' — ساختگی، نه بازار واقعی':''} | نماد: ${d.symbol}
کاراکتر پیام‌ها: ${d.characters} | ورودی تخمینی: ${d.estimated_input_tokens} توکن
روش: ${d.method} | توکنایزر: ندارد | شمارش دقیق نیست
خروجی، استدلال، کش و هزینهٔ واقعی: نامشخص | درخواست مدل: خیر
${d.note}`;
 }catch(e){result.textContent=e.message||'تخمین ناموفق بود.';}
 finally{button.disabled=false;}
};
node('workspace-test').onclick=async()=>{
 if(testing||!budgetAllowed||!modelReady)return;
 testing=true;syncTestButton();
 node('workspace-action').textContent='در انتظار آزمایش مدل؛ این درخواست از بودجهٔ مشترک کم می‌کند و ممکن است هزینه داشته باشد.';
 try{
  const d=await request('/api/model-test',{});
  node('workspace-action').textContent=d.ok?(d.message||'پاسخ واقعی مدل دریافت شد.'):(d.message||d.error||'آزمایش ناموفق بود.');
 }catch(e){node('workspace-action').textContent=(e&&e.message)||'آزمایش ناموفق بود؛ هیچ معامله‌ای درخواست نشد.';}
 finally{testing=false;await refresh();}
};
refresh();setInterval(refresh,5000);
})();
