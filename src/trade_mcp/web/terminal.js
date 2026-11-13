/* Local, read-only MT5 workspace. No third-party scripts, no broker tokens in storage. */
(() => {
'use strict';
const el = id => document.getElementById(id);
const make = (tag, text, cls) => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n; };
const fmt = v => v===null||v===undefined?'—':typeof v==='number'?Number(v.toFixed(8)).toLocaleString('en-US',{maximumFractionDigits:8}):String(v);
async function get(path){const r=await fetch(path);const d=await r.json();if(!r.ok||d.ok===false)throw new Error(d.message||d.error||'پاسخ نامعتبر');return d;}
const KEY='trade-desk-v1';
let saved={};try{saved=JSON.parse(localStorage.getItem(KEY)||'{}')||{};}catch(_){}
let charts=[],active=null,rows=[],timer=null,slowTimer=null,busy=false,oneColumn=!!saved.single,started=false;
let quoteBusy=false,liveFailures=0,lastDetail=0,lastWatch=0,liveGeneration=0;
const validRates=[250,500,1000,2000];
el('desk-rate').value=String(validRates.includes(saved.rate)?saved.rate:500);
el('desk-auto').checked=saved.live!==false;
const status=text=>el('desk-status').textContent=text;
function persist(){try{localStorage.setItem(KEY,JSON.stringify({single:oneColumn,rate:Number(el('desk-rate').value),live:el('desk-auto').checked,charts:charts.map(c=>c.settings())}));}catch(_){status('ذخیرهٔ چیدمان در مرورگر ممکن نیست.');}}
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
  const sell=make('button','SELL / فروش','chart-sell'),buy=make('button','BUY / خرید','chart-buy');
  sell.onclick=()=>{this.activate();openOrder(this.symbol,'sell');};buy.onclick=()=>{this.activate();openOrder(this.symbol,'buy');};
  this.sell=sell;this.buy=buy;bar.append(sell,buy);
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
 async load(mode='full'){
  if(this.pending){if(mode==='full')this.reloadAfter=true;return;}
  this.pending=true;const seq=++this.sequence,symbol=this.symbol,tf=this.tf;
  if(mode==='full'){this.quote.classList.remove('chart-error');this.quote.textContent='در حال خواندن '+symbol+' '+tf;}
  try{const selected=this.selected.size?[...this.selected].join(','):'ema';const q=new URLSearchParams({symbol,tf,selected});const d=await get((mode==='chart'&&this.data?'/api/chart?':'/api/terminal?')+q);if(seq!==this.sequence||symbol!==this.symbol||tf!==this.tf)return;
   const previous=this.data;this.data={...previous,...d};
   // A slower chart snapshot must not overwrite a newer quote.
   if(previous?.symbol===d.symbol&&previous?.quote?.received_at_ms&&!d.received_at_ms)this.data.quote=previous.quote;
   this.symbol=d.symbol;this.input.value=d.symbol;this.lastChart=Date.now();
   this.root.classList.remove('chart-stale');
   if(mode==='full'){this.updateQuote(this.data.quote);if(active===this)renderAccount(this.data);}
   this.draw();persist();
  }catch(e){if(seq!==this.sequence)return;this.root.classList.add('chart-stale');this.quote.textContent='قطع/خطا؛ دادهٔ قبلی ممکن است قدیمی باشد: '+e.message;this.quote.classList.add('chart-error');}
  finally{this.pending=false;if(this.reloadAfter&&this.root.isConnected){this.reloadAfter=false;this.load();}}
 }
 updateQuote(q){
  if(!this.data)return;const prev=this.data.quote?.bid;this.data.quote=q;
  this.sell.textContent='SELL '+fmt(q.bid);this.buy.textContent='BUY '+fmt(q.ask);
  this.lastQuote=Date.now();this.quote.classList.remove('chart-error');this.quote.title='زمان نمایش‌داده‌شده زمان دریافت است؛ نه تضمین زمان تیک بروکر.';
  this.quote.classList.toggle('quote-up',Number(q.bid)>Number(prev));this.quote.classList.toggle('quote-down',Number(q.bid)<Number(prev));
  this.quote.textContent=`${this.data.account.server?.includes('Mock')?'[SIMULATED] ':''}${this.symbol} ${this.tf} | Bid ${fmt(q.bid)} | Ask ${fmt(q.ask)} | spread ${fmt(q.spread_points)} pt | دریافت ${new Date(q.received_at_ms||Date.now()).toLocaleTimeString('fa-IR')}`;
  this.draw();
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
  // Live bid is a separate overlay, NOT an invented historical candle.
  if(this.offset===0)for(const [field,color,label] of [['bid','#60a5fa','BID'],['ask','#f9a870','ASK']]){
   const price=this.data.quote?.[field];if(price&&price>=lo-pad&&price<=hi+pad){c.strokeStyle=color;c.setLineDash([2,3]);c.beginPath();c.moveTo(0,y(price));c.lineTo(w-85,y(price));c.stroke();c.setLineDash([]);c.fillStyle=color;c.fillText(label+' '+fmt(price),Math.max(8,w-200),y(price)+(field==='ask'?-4:12));}
  }
  // Real broker positions, not hypothetical orders; never drag-to-trade.
  for(const p of this.data.positions.filter(p=>p.symbol===this.symbol))for(const [field,label,color] of [['price_open','POS '+p.ticket,'#e9b769'],['sl','SL','#ed6678'],['tp','TP','#41c69b']]){const v=p[field];if(!v||v<lo-pad||v>hi+pad)continue;c.strokeStyle=color;c.setLineDash([4,4]);c.beginPath();c.moveTo(0,y(v));c.lineTo(w-85,y(v));c.stroke();c.setLineDash([]);c.fillStyle=color;c.fillText(label+' '+fmt(v),8,y(v)-4);}
  const xFor=t=>{let best=0;for(let i=1;i<bars.length;i++)if(Math.abs(bars[i].time-t)<Math.abs(bars[best].time-t))best=i;return (best+.5)*dx;};
  c.save();c.beginPath();c.rect(0,18,w-85,plotH-18);c.clip();
  for(const l of this.lines){if(!l.points.length||(l.symbol&&l.symbol!==this.symbol)||(l.tf&&l.tf!==this.tf))continue;c.strokeStyle='#e6cc76';c.beginPath();if(l.type==='horizontal'){c.moveTo(0,y(l.points[0].price));c.lineTo(w-85,y(l.points[0].price));}else if(l.points.length===2){c.moveTo(xFor(l.points[0].time),y(l.points[0].price));c.lineTo(xFor(l.points[1].time),y(l.points[1].price));}c.stroke();}c.restore();
  lowerKeys.forEach((key,idx)=>{const top=plotH+idx*45,series=this.data.series[key],values=Object.values(series).flatMap(v=>v.slice(start,end)).filter(v=>v!==null&&Number.isFinite(v));if(!values.length)return;const min=['rsi','stochastic'].includes(key)?0:Math.min(...values),max=['rsi','stochastic'].includes(key)?100:Math.max(...values);c.strokeStyle='#24354a';c.beginPath();c.moveTo(0,top);c.lineTo(w,top);c.stroke();Object.values(series).forEach((v,i)=>seriesLine(v,p=>top+40-(p-min)/(max-min||1)*32,colors[i]));c.fillStyle='#a5b9ce';c.fillText(key.toUpperCase()+' '+fmt(Object.values(series)[0][end-1]),w-115,top+12);});
  if(this.cross&&this.cross.x<w-85&&this.cross.y<plotH){c.strokeStyle='#59728b';c.setLineDash([2,3]);c.beginPath();c.moveTo(this.cross.x,20);c.lineTo(this.cross.x,plotH);c.moveTo(0,this.cross.y);c.lineTo(w-85,this.cross.y);c.stroke();c.setLineDash([]);const i=Math.max(0,Math.min(bars.length-1,Math.floor(this.cross.x/dx))),b=bars[i];c.fillStyle='#d7e3f1';c.fillText(`O ${fmt(b.open)} H ${fmt(b.high)} L ${fmt(b.low)} C ${fmt(b.close)} V ${fmt(b.tick_volume)}`,8,plotH-3);}
  c.fillStyle='#7f96ad';c.fillText(new Date(bars[0].time*1000).toISOString().slice(0,16)+' → '+new Date(bars.at(-1).time*1000).toISOString().slice(0,16)+' feed clock',8,h-8);
 }
}
function renderAccount(d){
 const box=el('desk-account');box.replaceChildren();for(const [label,v] of [['سرور',d.account.server],['حساب',d.account.trade_mode],['ارز',d.account.currency],['موجودی',d.account.balance],['اکوییتی',d.account.equity],['مارجین آزاد',d.account.margin_free],['بودجهٔ تمرین',d.training_budget]])box.append(make('span',label+': '+fmt(v)));
 const target=el('desk-positions');target.replaceChildren();
 const table=make('table'),head=make('tr');for(const title of ['نوع','تیکت','نماد','جهت','حجم','ورود','SL','TP','سود','عملیات'])head.append(make('th',title));table.append(head);
 for(const [type,list] of [['پوزیشن',d.positions],['سفارش',d.orders]])for(const p of list){const tr=make('tr');for(const v of [type,p.ticket,p.symbol,p.type,p.volume,p.price_open,p.sl,p.tp,p.profit])tr.append(make('td',fmt(v)));
 const actions=make('td');if(type==='پوزیشن')for(const [action,title] of [['modify','SL/TP'],['close','بستن کامل']]){const b=make('button',title,'ghost');b.onclick=()=>{manualReset();el('manual-action').value=action;el('manual-ticket').value=String(p.ticket);el('manual-symbol').value=p.symbol;el('manual-sl').value=p.sl||'';el('manual-tp').value=p.tp||'';el('manual-volume').value='';el('manual-result').textContent='عملیات انتخاب شد؛ ابتدا پیش‌نمایش بگیرید.';openManualPanel();el('manual-preview').scrollIntoView({behavior:'smooth',block:'center'});};actions.append(b);}tr.append(actions);table.append(tr);}
 target.append(table);if(!d.positions.length&&!d.orders.length)target.append(make('p','پوزیشن یا سفارش بازی وجود ندارد.'));
}
function openManualPanel(){const dialog=el('order-dialog');el('order-dialog-body').append(el('manual-panel'));if(!dialog.open)dialog.showModal();}
el('order-dialog-close').onclick=()=>el('order-dialog').close();
el('order-dialog').addEventListener('close',()=>el('manual-slot').append(el('manual-panel')));
async function openOrder(symbol,side='buy'){
 el('manual-side').value=side;
 manualReset();el('manual-action').value='open';el('manual-symbol').value=symbol;el('manual-ticket').value='';el('manual-sl').value='';el('manual-tp').value='';el('manual-result').textContent='پیش‌نمایش سفارش هنوز ساخته نشده است.';openManualPanel();el('manual-ticket-info').textContent='دریافت قیمت و مشخصات…';
 try{const d=await get('/api/order-ticket?'+new URLSearchParams({symbol}));if(el('manual-symbol').value!==symbol)return;el('manual-symbol').value=d.symbol;el('manual-volume').value=d.spec.volume_min;el('manual-volume').step=d.spec.volume_step;el('manual-volume').min=d.spec.volume_min;el('manual-volume').max=d.spec.volume_max;
 el('manual-ticket-info').textContent=`${d.symbol} | ${d.account.server} | ${d.account.trade_mode} | Bid ${fmt(d.quote.bid)} / Ask ${fmt(d.quote.ask)} | حداقل حجم ${fmt(d.spec.volume_min)} | گام ${fmt(d.spec.volume_step)} | مجوز برنامه: ${d.trading_enabled?'روشن؛ نیازمند تأیید':'خاموش؛ فقط پیش‌نمایش'} | مجوز MCP در MT5: ${d.terminal.mcp_trade_allowed==null?'نامشخص':d.terminal.mcp_trade_allowed?'روشن':'خاموش؛ اجرای سفارش رد می‌شود'}`;
 }catch(e){el('manual-ticket-info').textContent=e.message;}
}
function drawWatch(){const box=el('desk-symbols');box.replaceChildren();const q=el('desk-search').value.trim().toUpperCase();for(const r of rows.filter(r=>((r.name||r.symbol||'')+' '+(r.description||'')).toUpperCase().includes(q)).slice(0,200)){const symbol=r.name||r.symbol,entry=make('div'),b=make('button',`${symbol}  ${fmt(r.bid)} / ${fmt(r.ask)}${r.visible===false?' • مخفی':''}`),chart=make('button','چارت');
 b.onclick=()=>openOrder(symbol);chart.onclick=()=>{if(!active)return;active.symbol=symbol;active.offset=0;active.load();persist();};entry.append(b,chart);
 if(r.visible===false){const add=make('button','افزودن به مارکت‌واچ MT5');add.onclick=async()=>{try{const res=await fetch('/api/marketwatch/select',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol,enable:true})});const d=await res.json();if(!res.ok||!d.ok)throw new Error(d.message||d.error);lastWatch=0;await refresh();}catch(e){status(e.message);}};entry.append(add);}
 box.append(entry);}}

async function refresh(){if(busy||!started)return;busy=true;el('desk-refresh').disabled=true;status('در حال خواندن داده…');try{await Promise.all(charts.map(c=>c.load('chart')));const now=Date.now();
 if(now-lastDetail>=10000){const d=await get('/api/account-state');for(const c of charts)if(c.data)Object.assign(c.data,d);if(active?.data)renderAccount(active.data);lastDetail=Date.now();}
 if(now-lastWatch>=30000){const d=await get('/api/marketwatch?all='+(el('desk-all-symbols').checked?'1':'0'));rows=d.symbols;drawWatch();lastWatch=Date.now();}
 status('کندل و اندیکاتور: هدف ۲ ثانیه • حساب: ۱۰ ثانیه • مارکت‌واچ عمومی: ۳۰ ثانیه • قیمت نمادهای باز: نرخ انتخابی');
 }catch(e){status('اتصال قطع یا داده نامعتبر: '+e.message);}finally{busy=false;el('desk-refresh').disabled=false;}}
function add(o){if(charts.length>=4){status('حداکثر چهار نمودار همزمان.');return;}const c=new Chart(o);charts.push(c);c.activate();c.load();persist();}
function layout(){el('desk-charts').classList.toggle('single',oneColumn);charts.forEach(c=>c.draw());}
async function init(){if(started){charts.forEach(c=>c.draw());return;}started=true;const list=Array.isArray(saved.charts)&&saved.charts.length?saved.charts.slice(0,4):[{symbol:'XAUUSD',tf:'M15'},{symbol:'XAUUSD',tf:'H1'}];for(const o of list)add(o);layout();await refresh();restartLive();}
el('desk-add').onclick=()=>add({symbol:active?.symbol||'XAUUSD',tf:'H1'});
el('desk-layout').onclick=()=>{oneColumn=!oneColumn;layout();persist();};el('desk-refresh').onclick=refresh;el('desk-search').oninput=drawWatch;el('desk-all-symbols').onchange=()=>{lastWatch=0;refresh();};
function visible(){return started&&el('desk-auto').checked&&el('tab-terminal').classList.contains('active')&&!document.hidden;}
async function pollQuotes(generation){
 if(!visible()||generation!==liveGeneration)return;
 const delay=Number(el('desk-rate').value),began=performance.now();
 if(quoteBusy){timer=setTimeout(()=>pollQuotes(generation),delay);return;}
 quoteBusy=true;
 try{const symbols=[...new Set(charts.filter(c=>c.data&&c.data.symbol===c.symbol).map(c=>c.symbol))];
 if(symbols.length){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),25000);let d;
 try{const r=await fetch('/api/quotes?'+new URLSearchParams({symbols:symbols.join(',')}),{signal:controller.signal});d=await r.json();if(!r.ok||!d.ok)throw new Error(d.message||d.error);}finally{clearTimeout(timeout);}
 if(generation!==liveGeneration||!visible())return;
 for(const c of charts){const q=d.quotes[c.symbol];if(q&&c.data?.symbol===c.symbol)c.updateQuote(q);else if(d.errors[c.symbol]){c.quote.classList.add('chart-error');c.quote.textContent='قیمت تازه دریافت نشد: '+d.errors[c.symbol];}}
 for(const row of rows){const q=d.quotes[row.symbol||row.name];if(q)Object.assign(row,q);}drawWatch();
 const elapsed=Math.round(performance.now()-began),failed=Object.keys(d.errors).length;
 liveFailures=failed?Math.min(liveFailures+1,5):0;
 el('desk-live').textContent=`${failed?'خطای دریافت '+failed+' نماد':'قیمت متصل'} • پاسخ ${elapsed} ms • هدف ${delay} ms • polling، نه تک‌تک تیک‌ها`;
 el('desk-live').classList.toggle('chart-error',!!failed);
 }
 }catch(e){liveFailures=Math.min(liveFailures+1,5);el('desk-live').textContent='قطع دریافت قیمت؛ تلاش مجدد با مکث: '+e.message;el('desk-live').classList.add('chart-error');for(const c of charts){c.quote.classList.add('chart-error');}}
 finally{quoteBusy=false;if(visible()&&generation===liveGeneration)timer=setTimeout(()=>pollQuotes(generation),Math.max(50,delay*Math.pow(2,liveFailures)-(performance.now()-began)));}
}
async function slowLoop(generation){if(!visible()||generation!==liveGeneration)return;const began=performance.now();try{await refresh();}finally{if(visible()&&generation===liveGeneration)slowTimer=setTimeout(()=>slowLoop(generation),Math.max(100,2000-(performance.now()-began)));}}
function restartLive(){liveGeneration++;clearTimeout(timer);clearTimeout(slowTimer);persist();if(visible()){const g=liveGeneration;timer=setTimeout(()=>pollQuotes(g),0);slowTimer=setTimeout(()=>slowLoop(g),2000);}else el('desk-live').textContent='دریافت خودکار متوقف است؛ قیمت‌های قبلی ممکن است قدیمی باشند.';}
el('desk-auto').onchange=restartLive;el('desk-rate').onchange=restartLive;
document.addEventListener('visibilitychange',restartLive);
new MutationObserver(restartLive).observe(el('tab-terminal'),{attributes:true,attributeFilter:['class']});
el('desk-reset').onclick=()=>{if(!confirm('خطوط رسم‌شده و چیدمان مرورگر پاک شوند؟'))return;for(const c of charts){c.sequence++;c.observer.disconnect();c.root.remove();}charts=[];active=null;oneColumn=false;add({symbol:'XAUUSD',tf:'M15'});add({symbol:'XAUUSD',tf:'H1'});layout();persist();};
el('desk-compare').onclick=async()=>{
 if(!active)return;const chart=active,box=el('desk-candles');box.textContent='در حال بازخوانی مستقیم…';
 try{const d=await get('/api/chart?'+new URLSearchParams({symbol:chart.symbol,tf:chart.tf,selected:[...chart.selected].join(',')||'ema',full:'1'}));if(chart.symbol!==d.symbol||chart.tf!==d.timeframe)return;
 chart.data={...chart.data,...d};chart.draw();box.replaceChildren();box.append(make('p',d.symbol+' / '+d.timeframe+' • آخرین ردیف ممکن است کندل در حال تشکیل باشد.'));
 const t=make('table'),head=make('tr');for(const title of ['زمان فید','Open','High','Low','Close','Tick volume'])head.append(make('th',title));t.append(head);
 for(const b of d.bars.slice(-5)){const tr=make('tr');for(const v of [new Date(b.time*1000).toISOString().slice(0,19).replace('T',' '),b.open,b.high,b.low,b.close,b.tick_volume])tr.append(make('td',fmt(v)));t.append(tr);}box.append(t);
 }catch(e){box.textContent=e.message;}
};
let manualProposal=null,manualRevision=0;
const manualReset=()=>{manualRevision++;manualProposal=null;el('manual-confirm').disabled=true;};
for(const n of document.querySelectorAll('[id^="manual-"]'))if(['INPUT','SELECT'].includes(n.tagName))n.addEventListener('input',manualReset);
el('manual-scenario').onclick=async()=>{
 manualReset();const revision=manualRevision,button=el('manual-scenario'),box=el('manual-result'),symbol=el('manual-symbol').value;button.disabled=true;box.textContent='در حال محاسبهٔ سناریو از چارت…';
 try{const d=await get('/api/analysis?'+new URLSearchParams({symbol}));if(revision!==manualRevision)return;const s=d.scenario||{},z=s.sizing||{};
 if(!['buy','sell'].includes(s.action)||!z.ok||!z.volume||!s.stop_loss||!s.take_profit_1)throw new Error(z.error||'سناریوی قابل ورود با بودجهٔ فعلی وجود ندارد؛ معامله نکنید.');
 el('manual-action').value='open';el('manual-symbol').value=d.symbol;el('manual-side').value=s.action;el('manual-volume').value=z.volume;el('manual-sl').value=s.stop_loss;el('manual-tp').value=s.take_profit_1;
 box.textContent='سناریوی قاعده‌محور به فرم منتقل شد؛ مدل زبانی نیست و سفارش ایجاد نشده است. برای قیمت فعلی و کنترل ریسک، پیش‌نمایش بگیرید.\n'+(d.summary_fa||'');
 }catch(e){box.textContent=e.message;}finally{button.disabled=false;}
};
el('manual-preview').onclick=async()=>{
 manualReset();const revision=manualRevision,button=el('manual-preview'),box=el('manual-result');button.disabled=true;box.textContent='در حال کنترل ریسک…';
 try{const payload={};for(const key of ['action','symbol','side','ticket','volume','risk','sl','tp'])payload[key]=el('manual-'+key).value;
 payload.auto_sl=el('manual-auto-sl').checked;
 if(payload.action==='close')delete payload.volume; // this UI offers full close only
 const r=await fetch('/api/manual/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),d=await r.json();
 if(revision!==manualRevision){box.textContent='فرم تغییر کرد؛ دوباره پیش‌نمایش بگیرید.';return;}
 if(!r.ok||!d.ok)throw new Error(d.message||d.error||'پیش‌نمایش ناموفق');
 const p=d.preview||{};
 if(p.kind==='open'){
  if(p.stop_loss!=null)el('manual-sl').value=p.stop_loss;
  if(p.take_profit!=null)el('manual-tp').value=p.take_profit;
  if(p.volume!=null)el('manual-volume').value=p.volume;
 }
 box.textContent=`نماد: ${p.symbol||payload.symbol} | جهت: ${p.side||payload.side} | عملیات: ${payload.action}\nشناسه: ${d.proposal_id}\nوضعیت: ${d.blocked?'مسدود؛ اجرا ممکن نیست':'در انتظار تأیید جداگانه'}\nحجم: ${fmt(p.volume)}\nورود تخمینی: ${fmt(p.estimated_entry)}\nSL: ${fmt(p.stop_loss??p.new_sl)}\nTP: ${fmt(p.take_profit??p.new_tp)}\nمارجین: ${fmt(p.estimated_margin)}\nزیان تخمینی پیش از هزینه: ${fmt(p.risk_amount)}\nدرصد بودجهٔ تمرینی: ${fmt(p.risk_percent_of_budget)}\n${d.auto_sl_note||''}\n${(d.blockers||[]).join('\n')}\n\nجزئیات:\n`+JSON.stringify(d,null,2);
 if(!d.blocked){manualProposal=d.proposal_id;el('manual-confirm').disabled=false;}
 }catch(e){box.textContent=e.message;}finally{button.disabled=false;}
};
el('manual-confirm').onclick=async()=>{
 if(!manualProposal||!confirm('همین پیشنهاد روی حساب دمو اجرا شود؟ این عملیات واقعاً پوزیشن را باز/بسته یا تغییر می‌دهد.'))return;
 const id=manualProposal;manualReset();try{const r=await fetch('/api/proposals/'+encodeURIComponent(id)+'/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),d=await r.json();el('manual-result').textContent=(d.executed?'MT5 اجرای سفارش را پذیرفت؛ نتیجهٔ ترمینال:\n':d.error==='mcp_trading_not_permitted'?'MT5 معامله از طریق MCP را مجاز نکرده است. Tools → Options → AI Assistant → Trading را بررسی کنید؛ تب MCP فقط اتصال سرور را تنظیم می‌کند. پیشنهاد ناموفق را دوباره تأیید نکنید؛ پس از اصلاح مجوز، پیش‌نمایش جدید بگیرید.\n':'اجرا نشد؛ علت:\n')+JSON.stringify(d,null,2);lastDetail=0;await refresh();}catch(e){el('manual-result').textContent=e.message;}
};
el('desk-analyze').onclick=async()=>{if(!active)return;const symbol=active.symbol,button=el('desk-analyze');button.disabled=true;el('desk-analysis').textContent='در حال تحلیل…';try{const d=await get('/api/analysis?symbol='+encodeURIComponent(symbol));const s=d.scenario||{},z=s.sizing||{};el('desk-analysis').textContent=(d.summary_fa||'')+'\n'+`ورود: ${fmt(s.entry_reference)} | SL: ${fmt(s.stop_loss)} | TP1: ${fmt(s.take_profit_1)}\nحجم: ${fmt(z.volume)} | بودجهٔ زیان: ${fmt(z.risk_amount)} | زیان تخمینی: ${fmt(z.actual_risk)}\n${z.error||''}\n${(z.warnings||[]).join('\n')}\n${(d.warnings||[]).join('\n')}\nتحلیل قاعده‌محور است، احتمال برد تضمین‌شده نیست.`;}catch(e){el('desk-analysis').textContent=e.message;}finally{button.disabled=false;}};
el('desk-llm').onclick=async()=>{
 if(!active||!confirm('خلاصهٔ نماد، قیمت، سناریو و هشدارها به سرویس مدل تنظیم‌شده ارسال می‌شود؛ شناسهٔ حساب و کلید ارسال نمی‌شود. ادامه؟'))return;
 const button=el('desk-llm');button.disabled=true;el('desk-analysis').textContent='در انتظار توضیح مدل…';
 try{const r=await fetch('/api/terminal/explain',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:active.symbol})});const d=await r.json();if(!r.ok||!d.ok)throw new Error(d.message||d.error);el('desk-analysis').textContent='توضیح مدل '+d.model+' (پیش‌بینی تضمینی نیست):\n'+d.text;}catch(e){el('desk-analysis').textContent=e.message;}finally{button.disabled=false;}
};
el('desk-training').onclick=()=>{if(active)el('tr-symbol').value=active.symbol;document.querySelector('[data-tab="training"]').click();};
document.querySelector('[data-tab="terminal"]').addEventListener('click',()=>requestAnimationFrame(init));
if(el('tab-terminal').classList.contains('active'))requestAnimationFrame(init);
const staleTimer=setInterval(()=>{if(!visible())return;let oldest=0;for(const c of charts){if(c.lastQuote){const age=Date.now()-c.lastQuote;oldest=Math.max(oldest,age);if(age>5000){c.quote.classList.add('chart-error');c.quote.title='از آخرین دریافت موفق '+Math.floor(age/1000)+' ثانیه گذشته است.';}}}if(quoteBusy&&oldest>5000){el('desk-live').textContent='دریافت کند است؛ آخرین قیمت موفق '+Math.floor(oldest/1000)+' ثانیه قبل. داده‌ها ممکن است قدیمی باشند.';el('desk-live').classList.add('chart-error');}},1000);
window.addEventListener('beforeunload',()=>{clearInterval(staleTimer);clearTimeout(timer);clearTimeout(slowTimer);liveGeneration++;persist();});
})();
