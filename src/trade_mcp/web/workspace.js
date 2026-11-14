/* Visible agent activity. Polling reads saved results only; model calls are explicit. */
(() => {
'use strict';
const node=id=>document.getElementById(id);
const make=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const busy=new Set();let refreshing=false,lease=null,modelLoaded=false;
const time=v=>v?new Date(v*1000).toLocaleString('fa-IR'):'هنوز اجرا نشده';
async function request(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
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
  node('workspace-status').textContent=`داده: ${d.backend}${d.backend==='mock'?' — ساختگی؛ دادهٔ واقعی نیست':''} | اجرای معامله: ${d.trading_enabled?'فعال؛ پیشنهاد OpenCode نیازمند تأیید شماست':'غیرفعال'} | مدل: ${d.model||'پیش‌فرض سرور'} / ${d.model_variant||'default'}\n${d.scheduler.running?'چرخهٔ ایجنت فعال است':'چرخهٔ خودکار خاموش است؛ دریافت قیمت به معنی معامله نیست.'}`;
  const cycle=d.page_cycle||{};
  node('workspace-cycle-state').textContent=`${cycle.active?'چرخه فعال':'چرخه متوقف'} | ${cycle.busy?'در حال پایان تحلیل':''} ${cycle.reason||''}`;
  if(!cycle.active)lease=null;
  node('workspace-cycle-start').disabled=!!cycle.active||!!cycle.busy;
  node('workspace-cycle-stop').disabled=!lease;
  if(!modelLoaded){node('workspace-model-id').value=d.model||'';node('workspace-variant').value=d.model_variant||'';node('workspace-free-fallback').checked=!!d.free_fallback;modelLoaded=true;}
  const select=node('workspace-cycle-agent'),chosen=select.value;
  const ids=(d.agents||[]).map(a=>a.id).join(',');
  if(select.dataset.ids!==ids){select.replaceChildren();for(const a of d.agents||[]){const option=make('option',a.name+' • '+a.symbols.join(', '));option.value=a.id;select.append(option);}select.value=chosen||d.agents[0]?.id||'';select.dataset.ids=ids;}
  const test=d.model_test||{};
  node('workspace-model').textContent=`آخرین آزمایش پاسخ مدل: ${test.state==='not_tested'?'هنوز انجام نشده':time(test.tested_at)}\n${test.message||'—'}${test.code?' ('+test.code+')':''}${test.response_model?'\nمدل پاسخ‌دهندهٔ تست: '+test.response_model+' / '+(test.response_variant||'default'):''}${(test.model_attempts||[]).map(a=>'\n'+a.model+' • '+a.status+(a.error?' • '+a.error:'')).join('')}`;
  const box=node('workspace-agents');box.replaceChildren();
  for(const a of d.agents||[]){
   const card=make('div');card.className='card';
   card.append(make('h3',a.name+' • '+a.symbols.join(', ')),make('p','آخرین اجرا: '+time(a.last_run)));
   const run=make('button',busy.has(a.id)?'در حال تحلیل…':'تحلیل با ایجنت — بدون اجرای معامله');run.className='primary';run.disabled=busy.has(a.id);
   run.onclick=async()=>{if(busy.has(a.id))return;busy.add(a.id);run.disabled=true;run.textContent='در حال تحلیل…';node('workspace-action').textContent='در انتظار نتیجهٔ ایجنت؛ هیچ تأییدی ارسال نمی‌شود.';
    try{const result=await request('/api/agents/'+encodeURIComponent(a.id),{action:'run_advisory'});node('workspace-action').textContent=result.ok?'گزارش ذخیره شد؛ نتیجه در کارت ایجنت نمایش داده می‌شود.':result.message||result.error||'اجرای ناموفق';}
    catch(_){node('workspace-action').textContent='دریافت نتیجه ناموفق بود؛ قبل از تکرار، آخرین گزارش را بررسی کنید.';}
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
   const confirm=make('button','تأیید و اجرای همین پیشنهاد');confirm.className='primary';confirm.disabled=!p.confirmable;
   if(!p.confirmable)card.append(make('p',p.status==='expired'?'پیشنهاد قدیمی است؛ پیش‌نمایش تازه لازم است.':'اجرا غیرفعال است یا این پیشنهاد دیگر در انتظار تأیید نیست.'));
   confirm.onclick=async()=>{
    if(!window.confirm(`اجرای واقعی همین پیشنهاد روی حساب متصل؟\n${p.id}\n${JSON.stringify(payload,null,2)}`))return;
    confirm.disabled=true;
    try{const result=await request('/api/proposals/'+encodeURIComponent(p.id)+'/confirm',{});node('workspace-action').textContent=result.ok?'هسته اجرای پیشنهاد را پذیرفت؛ نتیجه و پوزیشن‌ها را بررسی کنید.':result.message||result.error||'اجرا نشد';}
    catch(_){node('workspace-action').textContent='نتیجهٔ اجرا نامشخص است؛ پیش از تکرار، پوزیشن‌ها را بررسی کنید.';}
    finally{await refresh();}
   };
   card.append(confirm);(p.status==='pending'||p.status==='blocked'?proposals:history).append(card);
  }
  if(!d.proposals.some(p=>p.status==='pending'||p.status==='blocked'))proposals.append(make('p','پیشنهاد فعال جدیدی وجود ندارد. موارد قدیمی در تاریخچه‌اند.'));
  const runs=node('workspace-run-history');runs.replaceChildren(make('h3','تاریخچهٔ اجراهای ایجنت (اجراهای ثبت‌شده از این نسخه)'));
  for(const item of d.run_history||[]){const detail=make('details');detail.append(make('summary','گزارش تاریخی • '+time(item.created_at)+' • '+item.agent_id),make('pre','وضعیت زیر مربوط به زمان همین اجراست؛ وضعیت فعلی در فهرست پیشنهادهاست.\n'+describe(item.report||{})));runs.append(detail);}
 }catch(_){node('workspace-status').textContent='وضعیت پنل دریافت نشد؛ اتصال وب را بررسی کنید. دادهٔ قبلی ممکن است قدیمی باشد.';}
 finally{refreshing=false;}
}
async function stopCycle(beacon=false){
 if(!lease)return;const token=lease;lease=null;
 const body={action:'stop',lease:token};
 if(beacon){navigator.sendBeacon('/api/page-cycle',new Blob([JSON.stringify(body)],{type:'application/json'}));return;}
 try{await request('/api/page-cycle',body);}finally{await refresh();}
}
node('workspace-save-model').onclick=async()=>{
 const d=await request('/api/model-config',{model:node('workspace-model-id').value,variant:node('workspace-variant').value,free_fallback:node('workspace-free-fallback').checked});
 node('workspace-action').textContent=d.ok?'مدل وب اعمال شد؛ تنظیم چت OpenCode عوض نشد. اکنون پاسخ مدل را آزمایش کنید.':d.error;await refresh();
};
node('workspace-cycle-start').onclick=async()=>{
 if(document.hidden)return;
 const d=await request('/api/page-cycle',{action:'start',agent_id:node('workspace-cycle-agent').value,
  interval:Number(node('workspace-cycle-interval').value),consent_model_requests:node('workspace-cycle-consent').checked});
 if(d.ok)lease=d.lease;
 node('workspace-action').textContent=d.ok?'چرخهٔ مشورتی شروع شد؛ اجرای معامله فقط با تأیید جداگانه است.':d.error;await refresh();
};
node('workspace-cycle-stop').onclick=()=>stopCycle();
document.addEventListener('visibilitychange',()=>{if(document.hidden)stopCycle(true);});
window.addEventListener('pagehide',()=>stopCycle(true));
setInterval(async()=>{if(lease&&!document.hidden){try{const d=await request('/api/page-cycle',{action:'heartbeat',lease});if(!d.ok)lease=null;}catch(_){node('workspace-action').textContent='heartbeat ناموفق؛ چرخه بدون heartbeat معتبر متوقف خواهد شد.';}}},5000);
node('workspace-refresh').onclick=refresh;
node('workspace-test').onclick=async()=>{
 const b=node('workspace-test');b.disabled=true;node('workspace-action').textContent='در انتظار آزمایش مدل؛ ممکن است هزینهٔ سرویس داشته باشد.';
 try{const d=await request('/api/model-test',{});node('workspace-action').textContent=d.message||d.error;}
 catch(_){node('workspace-action').textContent='آزمایش ناموفق بود؛ هیچ معامله‌ای درخواست نشد.';}
 finally{b.disabled=false;await refresh();}
};
refresh();setInterval(refresh,5000);
})();
