/* Optional real-browser smoke test. npm install playwright-core outside the repo.
   CHROMIUM_PATH=/path/to/chromium NODE_PATH=/path/to/node_modules node scripts/browser_smoke.cjs
   Run against --mock console only. Does not submit orders. */
const assert = require('node:assert/strict');
const {chromium} = require('playwright-core');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,args:['--no-sandbox','--disable-dev-shm-usage'],headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1500,height:1050}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.CONSOLE_URL||'http://127.0.0.1:8792');
 await page.click('[data-tab="terminal"]');
 await page.waitForFunction(()=>document.querySelectorAll('.desk-quote').length===2&&[...document.querySelectorAll('.desk-quote')].every(n=>n.textContent.includes('Bid')));
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
 assert.equal(saved.charts[0].tf,'H4');assert.equal(saved.charts[0].lines.length,1);
 await page.setViewportSize({width:390,height:844});
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+2),false,'mobile horizontal overflow');
 assert.deepEqual(errors,[]);
 console.log('Browser smoke passed: charts, timeframe, drawings, persistence, tabs, indicators, training, mobile.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
