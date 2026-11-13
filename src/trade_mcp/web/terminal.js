/* Local, read-only MT5 workspace. No third-party scripts, no broker tokens in storage. */
(() => {
'use strict';
const el = id => document.getElementById(id);
const make = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n; };
const fmt = v => v===null||v===undefined?'—':typeof v==='number'?Number(v.toFixed(8)).toLocaleString('en-US',{maximumFractionDigits:8}):String(v);
async function get(path){const r=await fetch(path);const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.message||d.error||'پاسخ نامعتبر');return d;}
const KEY='trade-desk-v1';
let saved={};try{saved=JSON.parse(localStorage.getItem(KEY)||'{}')||{};}catch(_){}
let charts=[],active=null,rows=[],timer=null,busy=false,oneColumn=!!saved.single,started=false;
const status=text=>el('desk-status').textContent=text;
function persist(){try{localStorage.setItem(KEY,JSON.stringify({single:oneColumn,charts:charts.map(c=>c.settings())}));}catch(_){status('ذخیرهٔ چیدمان در مرورگر ممکن نیست.');}}
const colors=['#73b8ff','#ffbc65','#da8cff','#6ed7cf','#f680ab'];
class Chart {
 constructor(options={}){
  this.symbol=typeof options.symbol==='string'?options.symbol.slice(0,50):'XAUUSD';
  this.tf=['M1','M5','M15','M30','H1','H4','D1','W1'].includes(options.tf)?options.tf:'M15';
  this.selected=new Set((Array.isArray(options.indicators)?options.indicators:['ema','bollinger','rsi']).filter(x=>['ema','bollinger','rsi','macd','atr','adx','stochastic'].includes(x)));
  this.lines=Array.isArray(options.lines)?options.lines.filter(l=>['horizontal','trend'].includes(l.type)&&Array.isArray(l.points)&&l.points.length<=2&&l.points.every(p=>Number.isFinite(p.time)&&Number.isFinite(p.price))).slice(-30):[];
  this.count=100;this.offset=0;this.data=null;this.pending=false;this.sequence=0;this.draft=null;this.cross=null;
  this.root=make('div',undefined,'desk-chart');this.root.tabIndex=0;
  const bar=make('div',undefined,'desk-chart-bar');
  this.input=make('input');this.input.value=this.symbol;this.input.setAttribute('aria-label','نماد نمودار');
  const tf=make('select');for(const t of ['M1','M5','M15','M30','H1','H4','D1','W1']){const op=make('option',t);op.value=t;tf.append(op);}tf.value=this.tf;
  this.tool=make('select');for(const [v,t] of [['pan','جابه‌جایی / نشانگر'],['horizontal','خط افقی'],['trend','خط روند؛ دو کلیک']]){const op=make('option',t);op.value=v;this.tool.append(op);}
  const undo=make('button','حذف آخرین خط','ghost'),close=make('button','×','ghost');close.setAttribute('aria-label','بستن نمودار');
  bar.append(this.input,tf,this.tool,undo,close);
  const indicators=make('div',undefined,'desk-indicators');
  for(const [id,title] of [['ema','EMA'],['bollinger','BB'],['rsi','RSI'],['macd','MACD'],['atr','ATR'],['adx','ADX'],['stochastic','Stoch']]){const label=make('label'),box=make('input');box.type='checkbox';box.checked=this.selected.has(id);box.onchange=()=>{if(box.checked)this.selected.add(id);else this.selected.delete(id);persist();this.load();};label.append(box,make('span',title));indicators.append(label);}
  this.cv=make('canvas');this.cv.setAttribute('aria-label','نمودار کندلی با ابزار رسم و اندیکاتورها');this.quote=make('div','در حال بارگذاری…','desk-quote');
  this.root.append(bar,indicators,this.cv,this.quote,make('div','چرخ: زوم • کشیدن: تاریخچه • نشانگر: OHLC • خطوط فقط در مرورگر ذخیره می‌شوند؛ سفارش نیستند.','desk-chart-note'));
  el('desk-charts').append(this.root);
  this.root.addEventListener('pointerdown',()=>this.activate());
  this.input.onchange=()=>{this.symbol=this.input.value.trim()||'XAUUSD';this.offset=0;persist();this.load();};
  tf.onchange=()=>{this.tf=tf.value;this.offset=0;persist();this.load();};
  this.tool.onchange=()=>{this.draft=null;};
  undo.onclick=()=>{this.lines.pop();this.draft=null;persist();this.draw();};
  close.onclick=()=>{if(charts.length<=1)return;this.sequence++;this.root.remove();this.observer.disconnect();charts=charts.filter(c=>c!==this);if(active===this)charts[0].activate();persist();};
  this.cv.addEventListener('wheel',e=>{e.preventDefault();this.count=Math.min(280,Math.max(25,this.count+(e.deltaY>0?10:-10)));this.draw();},{passive:false});
  this.cv.onpointerdown=e=>{this.activate();if(!this.scale)return;const p=this.point(e);if(this.tool.value==='horizontal'){this.addLine({type:'horizontal',points:[p]});}else if(this.tool.value==='trend'){if(!this.draft)this.draft=p;else{this.addLine({type:'trend',points:[this.draft,p]});this.draft=null;}}else{this.drag={x:e.clientX,offset:this.offset};this.cv.setPointerCapture(e.pointerId);}};
  this.cv.onpointermove=e=>{const rect=this.cv.getBoundingClientRect();this.cross={x:e.clientX-rect.left,y:e.clientY-rect.top};if(this.drag&&this.data){const step=(rect.width-85)/this.count;this.offset=Math.max(0,Math.min(this.data.bars.length-this.count,this.drag.offset+Math.round((e.clientX-this.drag.x)/step)));}this.draw();};
  this.cv.onpointerup=()=>{this.drag=null;};this.cv.onpointercancel=()=>{this.drag=null;};this.cv.onpointerleave=()=>{this.cross=null;if(!this.drag)this.draw();};
  this.observer=new ResizeObserver(()=>this.draw());this.observer.observe(this.root);
 }
 settings(){return {symbol:this.symbol,tf:this.tf,indicators:[...this.selected],lines:this.lines};}
 activate(){active=this;charts.forEach(c=>c.root.classList.toggle('selected',c===this));if(this.data)renderAccount(this.data);}
 addLine(l){l.symbol=this.symbol;l.tf=this.tf;this.lines.push(l);this.lines=this.lines.slice(-30);persist();this.draw();}
 point(e){const r=this.cv.getBoundingClientRect(),s=this.scale;const i=Math.max(0,Math.min(s.bars.length-1,Math.floor((e.clientX-r.left)/s.dx)));return {time:s.bars[i].time,price:s.price(e.clientY-r.top)};}
 async load(){
  const seq=++this.sequence;this.quote.classList.remove('chart-error');this.quote.textContent='در حال خواندن '+this.symbol+' '+this.tf;
  try{const selected=this.selected.size?[...this.selected].join(','):'ema';const q=new URLSearchParams({symbol:this.symbol,tf:this.tf,selected});const d=await get('/api/terminal?'+q);if(seq!==this.sequence)return;this.data=d;this.symbol=d.symbol;this.input.value=d.symbol;this.quote.textContent=`${d.account.server?.includes('Mock')?'[SIMULATED] ':''}${d.symbol} ${d.timeframe} | Bid ${fmt(d.quote.bid)} | Ask ${fmt(d.quote.ask)} | ${new Date().toLocaleTimeString('fa-IR')} • تازه‌ترین کندل ممکن است باز باشد`;
   if(active===this)renderAccount(d);this.draw();persist();
  }catch(e){if(seq!==this.sequence)return;this.quote.textContent='قطع/خطا؛ دادهٔ قبلی ممکن است قدیمی باشد: '+e.message;this.quote.classList.add('chart-error');}
 }
 draw(){
  const rect=this.cv.getBoundingClientRect();if(rect.width<20||!this.data?.bars?.length)return;
  const w=rect.width,h=rect.height,dpr=window.devicePixelRatio||1;this.cv.width=Math.round(w*dpr);this.cv.height=Math.round(h*dpr);
  const c=this.cv.getContext('2d');c.scale(dpr,dpr);c.direction='ltr';c.textAlign='left';c.fillStyle='#090e15';c.fillRect(0,0,w,h);c.font='10px monospace';
  const end=this.data.bars.length-this.offset,start=Math.max(0,end-this.count),bars=this.data.bars.slice(start,end);if(!bars.length)return;
  const lo=Math.min(...bars.map(b=>b.low)),hi=Math.max(...bars.map(b=>b.high)),pad=(hi-lo||1)*.08;
  const lowerKeys=['rsi','macd','atr','adx','stochastic'].filter(k=>this.selected.has(k)&&this.data.series[k]);
  const plotH=lowerKeys.length?Math.max(140,h-32-lowerKeys.length*45):h-35;
  const y=p=>plotH-10-(p-lo+pad)/(hi-lo+2*pad)*(plotH-30),dx=(w-85)/bars.length;
  this.scale={bars,dx,price:py=>lo-pad+(plotH-10-py)/(plotH-30)*(hi-lo+2*pad)};
  for(let i=0;i<6;i++){const p=lo+(hi-lo)*i/5;c.strokeStyle='#1b2939';c.beginPath();c.moveTo(0,y(p));c.lineTo(w-80,y(p));c.stroke();c.fillStyle='#91a5bd';c.fillText(p.toFixed(this.data.spec.digits??2),w-78,y(p));}
  bars.forEach((b,i)=>{const x=(i+.5)*dx;c.strokeStyle=c.fillStyle=b.close>=b.open?'#41c69b':'#ed6678';c.beginPath();c.moveTo(x,y(b.high));c.lineTo(x,y(b.low));c.stroke();c.fillRect(x-dx*.3,Math.min(y(b.open),y(b.close)),Math.max(1,dx*.6),Math.max(1,Math.abs(y(b.open)-y(b.close))));});
  const seriesLine=(values,mapY,color)=>{c.strokeStyle=color;c.beginPath();let begun=false;values.slice(start,end).forEach((v,i)=>{if(v===null||!Number.isFinite(v)){begun=false;return;}const x=(i+.5)*dx,py=mapY(v);if(begun)c.lineTo(x,py);else c.moveTo(x,py);begun=true;});c.stroke();};
  c.save();c.beginPath();c.rect(0,0,w-85,plotH);c.clip();
  for(const key of ['ema','bollinger'])if(this.selected.has(key)&&this.data.series[key])Object.entries(this.data.series[key]).forEach(([name,values],i)=>{seriesLine(values,y,colors[i]);c.fillStyle=colors[i];c.fillText((key==='bollinger'?'BB':key)+' '+name,8+i*90,key==='bollinger'?27:14);});
  c.restore();
  // Real broker positions, not hypothetical orders; never drag-to-trade.
  for(const p of this.data.positions.filter(p=>p.symbol===this.symbol))for(const [field,label,color] of [['price_open','POS '+p.ticket,'#e9b769'],['sl','SL','#ed6678'],['tp','TP','#41c69b']]){const v=p[field];if(!v||v<lo-pad||v>hi+pad)continue;c.strokeStyle=color;c.setLineDash([4,4]);c.beginPath();c.moveTo(0,y(v));c.lineTo(w-85,y(v));c.stroke();c.setLineDash([]);c.fillStyle=color;c.fillText(label+' '+fmt(v),8,y(v)-4);}
  const xFor=t=>{let best=0;for(let i=1;i<bars.length;i++)if(Math.abs(bars[i].time-t)<Math.abs(bars[best].time-t))best=i;return (best+.5)*dx;};
  c.save();c.beginPath();c.rect(0,18,w-85,plotH-18);c.clip();
  for(const l of this.lines){if(!l.points.length||(l.symbol&&l.symbol!==this.symbol)||(l.tf&&l.tf!==this.tf))continue;c.strokeStyle='#e6cc76';c.beginPath();if(l.type==='horizontal'){c.moveTo(0,y(l.points[0].price));c.lineTo(w-85,y(l.points[0].price));}else if(l.points.length===2){c.moveTo(xFor(l.points[0].time),y(l.points[0].price));c.lineTo(xFor(l.points[1].time),y(l.points[1].price));}c.stroke();}c.restore();
  lowerKeys.forEach((key,idx)=>{const top=plotH+idx*45,series=this.data.series[key],values=Object.values(series).flatMap(v=>v.slice(start,end)).filter(v=>v!==null&&Number.isFinite(v));if(!values.length)return;const min=['rsi','stochastic'].includes(key)?0:Math.min(...values),max=['rsi','stochastic'].includes(key)?100:Math.max(...values);c.strokeStyle='#24354a';c.beginPath();c.moveTo(0,top);c.lineTo(w,top);c.stroke();Object.values(series).forEach((v,i)=>seriesLine(v,p=>top+40-(p-min)/(max-min||1)*32,colors[i]));c.fillStyle='#a5b9ce';c.fillText(key.toUpperCase()+' '+fmt(Object.values(series)[0][end-1]),w-115,top+12);});
  if(this.cross&&this.cross.x<w-85&&this.cross.y<plotH){c.strokeStyle='#59728b';c.setLineDash([2,3]);c.beginPath();c.moveTo(this.cross.x,20);c.lineTo(this.cross.x,plotH);c.moveTo(0,this.cross.y);c.lineTo(w-85,this.cross.y);c.stroke();c.setLineDash([]);const i=Math.max(0,Math.min(bars.length-1,Math.floor(this.cross.x/dx))),b=bars[i];c.fillStyle='#d7e3f1';c.fillText(`O ${fmt(b.open)} H ${fmt(b.high)} L ${fmt(b.low)} C ${fmt(b.close)} V ${fmt(b.tick_volume)}`,8,plotH-3);}
  c.fillStyle='#7f96ad';c.fillText(new Date(bars[0].time*1000).toISOString().slice(0,16)+' → '+new Date(bars.at(-1).time*1000).toISOString().slice(0,16)+' UTC display',8,h-8);
 }
}
function renderAccount(d){
 const box=el('desk-account');box.replaceChildren();for(const [label,v] of [['سرور',d.account.server],['حساب',d.account.trade_mode],['ارز',d.account.currency],['موجودی',d.account.balance],['اکوییتی',d.account.equity],['مارجین آزاد',d.account.margin_free],['بودجهٔ تمرین',d.training_budget]])box.append(make('span',label+': '+fmt(v)));
 const target=el('desk-positions');target.replaceChildren();
 const table=make('table'),head=make('tr');for(const title of ['نوع','تیکت','نماد','جهت','حجم','ورود','SL','TP','سود'])head.append(make('th',title));table.append(head);
 for(const [type,list] of [['پوزیشن',d.positions],['سفارش',d.orders]])for(const p of list){const tr=make('tr');for(const v of [type,p.ticket,p.symbol,p.type,p.volume,p.price_open,p.sl,p.tp,p.profit])tr.append(make('td',fmt(v)));table.append(tr);}
 target.append(table);if(!d.positions.length&&!d.orders.length)target.append(make('p','پوزیشن یا سفارش بازی وجود ندارد.'));
}
function drawWatch(){const box=el('desk-symbols');box.replaceChildren();const q=el('desk-search').value.trim().toUpperCase();for(const r of rows.filter(r=>(r.name||r.symbol||'').toUpperCase().includes(q)).slice(0,100)){const symbol=r.name||r.symbol,b=make('button',`${symbol}  ${fmt(r.bid)} / ${fmt(r.ask)}`);b.onclick=()=>{if(!active)return;active.symbol=symbol;active.offset=0;active.load();persist();};box.append(b);}}
async function refresh(){if(busy||!started)return;busy=true;el('desk-refresh').disabled=true;status('در حال خواندن داده…');try{await Promise.all(charts.map(c=>c.load()));const d=await get('/api/marketwatch');rows=d.symbols;drawWatch();status('آخرین تلاش: '+new Date().toLocaleTimeString('fa-IR')+' • به‌روزرسانی دوره‌ای، نه استریم تیک؛ خطای هر نمودار کنار همان نمودار نمایش داده می‌شود.');}catch(e){status('اتصال قطع یا داده نامعتبر: '+e.message);}finally{busy=false;el('desk-refresh').disabled=false;}}
function add(o){if(charts.length>=4){status('حداکثر چهار نمودار همزمان.');return;}const c=new Chart(o);charts.push(c);c.activate();c.load();persist();}
function layout(){el('desk-charts').classList.toggle('single',oneColumn);charts.forEach(c=>c.draw());}
async function init(){if(started){charts.forEach(c=>c.draw());return;}started=true;const list=Array.isArray(saved.charts)&&saved.charts.length?saved.charts.slice(0,4):[{symbol:'XAUUSD',tf:'M15'},{symbol:'XAUUSD',tf:'H1'}];for(const o of list)add(o);layout();await refresh();}
el('desk-add').onclick=()=>add({symbol:active?.symbol||'XAUUSD',tf:'H1'});
el('desk-layout').onclick=()=>{oneColumn=!oneColumn;layout();persist();};el('desk-refresh').onclick=refresh;el('desk-search').oninput=drawWatch;
el('desk-auto').onchange=()=>{clearInterval(timer);if(el('desk-auto').checked)timer=setInterval(()=>{if(el('tab-terminal').classList.contains('active')&&!document.hidden)refresh();},15000);};
el('desk-reset').onclick=()=>{if(!confirm('خطوط رسم‌شده و چیدمان مرورگر پاک شوند؟'))return;for(const c of charts){c.sequence++;c.observer.disconnect();c.root.remove();}charts=[];active=null;oneColumn=false;add({symbol:'XAUUSD',tf:'M15'});add({symbol:'XAUUSD',tf:'H1'});layout();persist();};
el('desk-analyze').onclick=async()=>{if(!active)return;const symbol=active.symbol,button=el('desk-analyze');button.disabled=true;el('desk-analysis').textContent='در حال تحلیل…';try{const d=await get('/api/analysis?symbol='+encodeURIComponent(symbol));const s=d.scenario||{},z=s.sizing||{};el('desk-analysis').textContent=(d.summary_fa||'')+'\n'+`ورود: ${fmt(s.entry_reference)} | SL: ${fmt(s.stop_loss)} | TP1: ${fmt(s.take_profit_1)}\nحجم: ${fmt(z.volume)} | بودجهٔ زیان: ${fmt(z.risk_amount)} | زیان تخمینی: ${fmt(z.actual_risk)}\n${z.error||''}\n${(z.warnings||[]).join('\n')}\n${(d.warnings||[]).join('\n')}\nتحلیل قاعده‌محور است، احتمال برد تضمین‌شده نیست.`;}catch(e){el('desk-analysis').textContent=e.message;}finally{button.disabled=false;}};
el('desk-llm').onclick=async()=>{
 if(!active||!confirm('خلاصهٔ نماد، قیمت، سناریو و هشدارها به سرویس مدل تنظیم‌شده ارسال می‌شود؛ شناسهٔ حساب و کلید ارسال نمی‌شود. ادامه؟'))return;
 const button=el('desk-llm');button.disabled=true;el('desk-analysis').textContent='در انتظار توضیح مدل…';
 try{const r=await fetch('/api/terminal/explain',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:active.symbol})});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.message||d.error);el('desk-analysis').textContent='توضیح مدل '+d.model+' (پیش‌بینی تضمینی نیست):\n'+d.text;}catch(e){el('desk-analysis').textContent=e.message;}finally{button.disabled=false;}
};
el('desk-training').onclick=()=>{if(active)el('tr-symbol').value=active.symbol;document.querySelector('[data-tab="training"]').click();};
document.querySelector('[data-tab="terminal"]').addEventListener('click',()=>requestAnimationFrame(init));
if(el('tab-terminal').classList.contains('active'))requestAnimationFrame(init);
window.addEventListener('beforeunload',()=>{clearInterval(timer);persist();});
})();
