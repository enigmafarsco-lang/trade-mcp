/* Visible agent activity. Polling reads saved results only; model calls are explicit. */
(() => {
'use strict';
const node=id=>document.getElementById(id);
const make=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
const busy=new Set();let refreshing=false;
const time=v=>v?new Date(v*1000).toLocaleString('fa-IR'):'هنوز اجرا نشده';
async function request(path,body){const r=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
function describe(report){
 const lines=[`مدل واقعاً استفاده شد: ${report.used_llm?'بله':'خیر'} | پیشنهادهای این اجرا: ${(report.proposals||[]).length}`];
 for(const d of report.decisions||[]){
  lines.push(`${d.symbol}: ${{buy:'خرید',sell:'فروش',wait:'انتظار'}[d.action]||'نامشخص'} | منبع: ${d.source||'—'}`);
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
  node('workspace-status').textContent=`داده: ${d.backend}${d.backend==='mock'?' — ساختگی؛ دادهٔ واقعی نیست':''} | اجرای معامله: ${d.trading_enabled?'فعال؛ پیشنهاد OpenCode نیازمند تأیید شماست':'غیرفعال'} | مدل: ${d.model||'پیش‌فرض سرور'}\n${d.scheduler.running?'چرخهٔ ایجنت فعال است':'چرخهٔ خودکار خاموش است؛ دریافت قیمت به معنی معامله نیست.'}`;
  const test=d.model_test||{};
  node('workspace-model').textContent=`آخرین آزمایش پاسخ مدل: ${test.state==='not_tested'?'هنوز انجام نشده':time(test.tested_at)}\n${test.message||'—'}${test.code?' ('+test.code+')':''}`;
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
  const proposals=node('workspace-proposals');proposals.replaceChildren();
  proposals.append(make('h3','پیشنهادها و سابقه — سفارش جدید خودکار اجرا نمی‌شود'));
  for(const p of d.proposals||[]){
   const payload=p.payload||{},card=make('details');
   card.append(make('summary',`${p.id} | ${payload.symbol||'—'} | ${p.status==='expired'?'منقضی؛ قابل تأیید نیست':p.status} | ${time(p.created_at)}`));
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
   card.append(confirm);proposals.append(card);
  }
  if(!d.proposals.length)proposals.append(make('p','هیچ پیشنهاد ثبت‌شده‌ای وجود ندارد.'));
 }catch(_){node('workspace-status').textContent='وضعیت پنل دریافت نشد؛ اتصال وب را بررسی کنید. دادهٔ قبلی ممکن است قدیمی باشد.';}
 finally{refreshing=false;}
}
node('workspace-refresh').onclick=refresh;
node('workspace-test').onclick=async()=>{
 const b=node('workspace-test');b.disabled=true;node('workspace-action').textContent='در انتظار آزمایش مدل؛ ممکن است هزینهٔ سرویس داشته باشد.';
 try{const d=await request('/api/model-test',{});node('workspace-action').textContent=d.message||d.error;}
 catch(_){node('workspace-action').textContent='آزمایش ناموفق بود؛ هیچ معامله‌ای درخواست نشد.';}
 finally{b.disabled=false;await refresh();}
};
refresh();setInterval(refresh,5000);
})();
