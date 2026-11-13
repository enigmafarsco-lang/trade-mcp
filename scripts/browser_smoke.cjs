/* Optional real-browser smoke test. npm install playwright-core outside the repo.
   CHROMIUM_PATH=/path/to/chromium NODE_PATH=/path/to/node_modules node scripts/browser_smoke.cjs
   Run against --mock console only. Does not submit orders. */
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage'],headless:true});
 try{
 let quoteRequests=0,liveInFlight=0,maxLiveInFlight=0;
 const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.url().includes('/api/quotes?')){quoteRequests++;liveInFlight++;maxLiveInFlight=Math.max(maxLiveInFlight,liveInFlight);}});
 const done=r=>{if(r.url().includes('/api/quotes?'))liveInFlight--;};page.on('requestfinished',done);page.on('requestfailed',done);
 await page.goto(process.env.CONSOLE_URL||'http://127.0.0.1:8792');
 await page.click('[data-tab="terminal"]');
 await page.waitForFunction(()=>document.querySelectorAll('.desk-quote').length===2&&[...document.querySelectorAll('.desk-quote')].every(n=>n.textContent.includes('Bid')));
 await page.waitForFunction(()=>document.getElementById('desk-live').textContent.includes('پاسخ'));
 await page.selectOption('#desk-rate','250');
 await page.waitForTimeout(1100);assert.ok(quoteRequests>=2,'fast quote polling is active');
 await page.uncheck('#desk-auto');await page.waitForTimeout(350);const paused=quoteRequests;await page.waitForTimeout(600);assert.equal(quoteRequests,paused,'pause stops quote polling');
 await page.check('#desk-auto');
 await page.route('**/api/quotes?*',async route=>{await new Promise(r=>setTimeout(r,400));await route.continue();});
 await page.waitForTimeout(1000);await page.unrouteAll({behavior:'wait'});
 assert.equal(maxLiveInFlight,1,'quote requests must not overlap even with a slow response');
 await page.route('**/api/quotes?*',route=>route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({ok:false,message:'simulated disconnect'})}));
 await page.waitForFunction(()=>document.getElementById('desk-live').textContent.includes('قطع دریافت'));
 await page.unrouteAll({behavior:'wait'});
 await page.waitForFunction(()=>document.getElementById('desk-live').textContent.includes('قیمت متصل'));

 await page.locator('.desk-chart').first().locator('select').first().selectOption('H4');
 await page.waitForFunction(()=>document.querySelector('.desk-quote').textContent.includes('H4 | Bid'));
 // Draw a horizontal line, and verify layout/line persistence, not just rendering.
 await page.locator('.desk-chart').first().locator('select').nth(1).selectOption('horizontal');
 const box=await page.locator('.desk-chart canvas').first().boundingBox();
 await page.mouse.click(box.x+box.width/2,box.y+100);
 let saved=JSON.parse(await page.evaluate(()=>localStorage.getItem('trade-desk-v1')));
 assert.equal(saved.charts[0].lines.length,1);
 await page.click('#desk-add');await page.waitForFunction(()=>document.querySelectorAll('.desk-chart').length===3);
 await page.click('#desk-analyze');await page.waitForFunction(()=>document.getElementById('desk-analysis').textContent.includes('حجم:'));
 for(const tab of ['overview','analysis','training','plugins','agents','journal','settings'])await page.click(`[data-tab="${tab}"]`);
 await page.click('[data-tab="plugins"]');await page.click('#plugin-run');
 await page.waitForFunction(()=>document.getElementById('plugin-result').textContent.includes('macd'));
 await page.click('[data-tab="training"]');await page.click('#tr-run');
 await page.waitForFunction(()=>document.getElementById('tr-status').textContent.includes('انجام شد'));
 await page.reload();await page.click('[data-tab="terminal"]');
 await page.waitForFunction(()=>document.querySelectorAll('.desk-chart').length===3);
 saved=JSON.parse(await page.evaluate(()=>localStorage.getItem('trade-desk-v1')));
 assert.equal(saved.rate,250);assert.equal(saved.charts[0].tf,'H4');assert.equal(saved.charts[0].lines.length,1);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+2),false,'mobile horizontal overflow');
 assert.deepEqual(errors,[]);
 console.log('Browser smoke passed: live rate/pause/non-overlap, charts, timeframe, drawings, persistence, tabs, indicators, training, mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
