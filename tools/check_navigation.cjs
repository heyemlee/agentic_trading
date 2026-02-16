// Offline browser regression: node tools/check_navigation.cjs (requires Playwright + Chrome).
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const { execFileSync } = require('node:child_process');
// Render static templates with the production formatter, without starting the API or lifespan.
const layouts = JSON.parse(execFileSync(path.join(root, '.venv/bin/python'), ['-c', `
import json
from pathlib import Path
from agentic_trading.api.app import ConsoleFiles
ui = Path('ui')
files = ConsoleFiles(ui)
print(json.dumps({name: files.primary_templates((ui / (name + '.html')).read_text(), name)
                  for name in ('index', 'portfolio', 'advisor', 'more', 'risk', 'activity',
                               'evaluation', 'profile', 'notifications', 'system', 'approvals', 'research', 'lab')}))
`], {cwd: root, encoding:'utf8'}));
(async () => {
 const server = http.createServer(async (req, res) => {
   const file = path.resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
   if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
   try {
     const body = layouts[path.basename(file, '.html')] || await fs.readFile(file);
     const mime = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.png':'image/png', '.svg':'image/svg+xml'};
     res.writeHead(200, {'Content-Type': mime[path.extname(file)] || 'application/octet-stream'}).end(body);
   } catch { res.writeHead(404).end(); }
 });
 await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
 const origin = 'http://127.0.0.1:' + server.address().port;
 let browser;
 try {
 browser = await chromium.launch({channel:process.env.CT_BROWSER_CHANNEL || 'chrome',headless:true});
 const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 const page = await context.newPage();
 const errors = [], calls=[];
 page.on('pageerror', e=> errors.push(e.message));
 page.on('console', e=> {if(e.type()==='error' && !e.text().includes('401 (Unauthorized)')) errors.push(e.text())});
 const payload = {
 '/auth/session': {authenticated:true, csrf_token:'test-only', username:'preview'},
 '/health': {status:'READY',paused:false,market:{},warnings:[]},
 '/portfolio': {accounts:{},buckets:{},errors:{},as_of:'2026-09-16'},
 '/proposals': [],
 '/orders': {fills:[],open_orders:[],tracked_orders:[]},
 '/advisor': {stale:false,snapshot:{accounts:[],candidates:[]},jobs:[],decisions:[],conversations:[],notes:{text:'',revision:0}},
 '/advisor/conversations/default':{messages:[]},
 '/research/jobs':[], '/research':[],
 };
 const fixtureContext = { URLSearchParams, window: { location: { search: '?demo=1' } } };
 require('node:vm').runInNewContext(await fs.readFile(path.join(root, 'ui/assets/data.js'), 'utf8'), fixtureContext);
 for (const [key, endpoint] of Object.entries({risk:'/risk', microLive:'/micro-live',
   schedule:'/system/schedule', operations:'/system/operations', notifications:'/system/notifications',
   authSessions:'/auth/sessions', pushDevices:'/notifications/devices', evaluation:'/evaluation', activity:'/activity'})) {
   payload[endpoint] = fixtureContext.window.CT[key];
 }
 let delay=1800, denied=false;
 await page.route('**/*', async route=>{
  const url = new URL(route.request().url());
  if(url.pathname.startsWith('/ui/')) {
    if (/assets\/pages\/(portfolio|advisor|more)\.js/.test(url.pathname)) await new Promise(r=>setTimeout(r,900));
    return route.continue();
  }
  if(url.pathname==='/favicon.ico') return route.fulfill({status:204});
  calls.push({path:url.pathname,method:route.request().method()});
  await new Promise(r=>setTimeout(r,delay));
  await route.fulfill({status:denied?401:200,contentType:'application/json',body:JSON.stringify(denied?{detail:'login required'}:payload[url.pathname]||{})});
 });
 await page.goto(origin + '/ui/index.html', {waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>window.CT_NAV && !document.getElementById('content').hidden);
 assert.equal(await page.locator('.page-skeleton').count(),0);
 assert.equal(await page.locator('#startup').isVisible(),true);
 await page.locator('.startup-mark').evaluate(el => el.decode());
 assert.equal(await page.locator('.tabbar').isVisible(),false, 'cold start must cover intermediate UI');
 const startupTitle = await page.locator('#startup-title').evaluate(el => ({text:el.textContent.trim(), height:el.getBoundingClientRect().height}));
 assert.equal(startupTitle.text, 'Agentic Trading');
 assert.ok(startupTitle.height < 50, 'startup product name must stay on one line');
 if (process.env.CT_UI_CAPTURE_DIR) {
   await page.evaluate(() => I18N.set('zh'));
   await page.waitForTimeout(350);
   const folder = path.resolve(process.env.CT_UI_CAPTURE_DIR);
   await fs.mkdir(folder, {recursive:true});
   await page.screenshot({path:path.join(folder, 'startup-phone.png')});
   const mark = await fs.readFile(path.join(root, 'ui/icons/mark.svg'), 'utf8');
   const markup = (await page.locator('#startup').evaluate(el => el.outerHTML))
     .replace('src="icons/mark.svg"', 'src="data:image/svg+xml;base64,' + Buffer.from(mark).toString('base64') + '"');
   const styles = await Promise.all(['console.css', 'startup.css'].map(name => fs.readFile(path.join(root, 'ui/assets', name), 'utf8')));
   await fs.writeFile(path.join(folder, 'startup-preview.html'), '<!doctype html><html lang="zh-Hans"><head>' +
     '<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">' +
     '<title>Agentic Trading · 启动页预览</title><style>' + styles.join('\n') + '</style></head><body>' + markup + '</body></html>');
 }

 const times=[];
 for(const tab of ['portfolio','advisor','more','dashboard','portfolio','advisor']) {
   const t = await page.evaluate(tab=>{
    const before = performance.now();
    document.querySelector('[data-od-id="tab-'+tab+'"]').click();
    return {ms:performance.now()-before,path:location.pathname,hidden:document.getElementById('content').hidden};
   },tab);
   times.push(t); assert.equal(t.hidden,false);assert.ok(t.ms<100);
   assert.ok(t.path.endsWith((tab==='dashboard'?'index':tab)+'.html'));
 }
 await page.waitForTimeout(2400);
 await page.waitForSelector('#startup', {state:'detached'});
 assert.equal(await page.locator('.tabbar').isVisible(),true);
 assert.match((await page.locator('.brand').innerText()).replace(/\s+/g, ' '), /Agentic Trading/);
 await page.locator('#chat-input').fill('Draft stays across tabs');
 await page.locator('[data-od-id="tab-portfolio"]').click();
 await page.locator('[data-od-id="tab-advisor"]').click();
 assert.equal(await page.locator('#chat-input').inputValue(),'Draft stays across tabs');
 await page.locator('#view-discover').click();
 await page.locator('[data-od-id="tab-more"]').click();
 await page.locator('[data-od-id="tab-advisor"]').click();
 assert.equal(await page.locator('#view-discover').getAttribute('aria-selected'),'true');
 await page.goBack();assert.ok(page.url().endsWith('more.html'));
 await page.goForward();assert.ok(page.url().endsWith('advisor.html'));
 assert.equal(await page.locator('.topnav').count(),1);
 assert.equal(await page.locator('.tabbar').count(),1);
 assert.equal(await page.locator('#content').count(),1);
 assert.equal(await page.locator('#startup').count(),0, 'route changes must not replay the startup');
 await page.locator('[data-od-id="tab-portfolio"]').click();
 await page.locator('a[href="advisor.html?prompt=holdings-risk"]').click();
 assert.ok(page.url().includes('prompt=holdings-risk'));
 // Typed text is preserved, including when opening an analysis deep link.
 assert.equal(await page.locator('#chat-input').inputValue(),'Draft stays across tabs');
 await page.locator('[data-od-id="tab-dashboard"]').click();

 // More tools must switch synchronously even while their first reads are pending.
 await page.locator('[data-od-id="tab-more"]').click();
 const documentToken = await page.evaluate(() => window.navigationTestToken = crypto.randomUUID());
 for (const name of ['risk', 'activity', 'evaluation', 'profile', 'notifications', 'system']) {
   const result = await page.evaluate(name => {
     const before = performance.now();
     document.querySelector('#content a[href="' + name + '.html"]').click();
     return {ms:performance.now()-before, path:location.pathname,
       hidden:document.getElementById('content').hidden, skeleton:!!document.querySelector('.page-skeleton')};
   }, name);
   assert.ok(result.path.endsWith(name + '.html'));
   assert.equal(result.hidden, false); assert.equal(result.skeleton, false); assert.ok(result.ms < 100);
   times.push(result);
   await page.waitForFunction(() => !document.getElementById('content').inert);
   await page.locator('#back-link').click();
   await page.waitForURL('**/more.html');
   assert.equal(await page.evaluate(() => window.navigationTestToken), documentToken);
   assert.equal(await page.locator('#content').isVisible(), true);
   assert.equal(await page.locator('#startup, .page-skeleton').count(), 0);
 }
 await page.locator('#content a[href="profile.html"]').click();
 await page.locator('#profile-notes').fill('Personal draft survives returning to More');
 await page.locator('#back-link').click(); await page.waitForURL('**/more.html');
 await page.goForward(); await page.waitForURL('**/profile.html');
 assert.equal(await page.locator('#profile-notes').inputValue(), 'Personal draft survives returning to More');
 await page.locator('[data-od-id="tab-dashboard"]').click();

 assert.equal(calls.filter(c=>c.path==='/auth/session').length,1);
 assert.equal(calls.filter(c=>c.method==='POST').length,0);
 assert.equal(await page.evaluate(()=>performance.getEntriesByType('navigation').length),1);
 delay=0;denied=true;
 await page.evaluate(()=>CT_API.readOnce('portfolio'));
 assert.equal(await page.locator('#content').isVisible(),false);
 await page.locator('[data-od-id="tab-advisor"]').click();
 assert.equal(await page.locator('#content').isVisible(),false);
 assert.equal(await page.locator('#chat-input').inputValue(),'');
 await page.locator('[data-od-id="tab-more"]').click();
 await page.evaluate(() => document.querySelector('#content a[href="profile.html"]').click());
 assert.equal(await page.locator('#profile-notes').inputValue(), '');
 assert.equal(await page.locator('#content').isVisible(), false);
 calls.length = 0;
 await page.goto(origin + '/ui/index.html?demo=1');
 for (const tab of ['advisor', 'portfolio', 'more', 'dashboard']) {
   await page.locator('[data-od-id="tab-' + tab + '"]').click();
   assert.ok(page.url().includes('demo=1'));
   assert.equal(await page.locator('#content').isVisible(), true);
 }
 for (const name of ['risk', 'activity', 'evaluation', 'profile', 'notifications', 'system', 'approvals', 'research', 'lab']) {
   await page.evaluate(name => {
     const link = document.createElement('a'); link.href = name + '.html?demo=1';
     document.body.append(link); link.click(); link.remove();
   }, name);
   await page.waitForFunction(() => !document.getElementById('content').inert);
   assert.ok(page.url().includes(name + '.html?demo=1'));
   assert.equal(await page.locator('#content').isVisible(), true);
   assert.equal(await page.locator('#startup, .page-skeleton').count(), 0);
   assert.equal(await page.locator('.topnav').count(), 1);
 }
 await page.locator('[data-od-id="tab-more"]').click();
 for (const ticker of ['VTI', 'VOO']) {
   await page.evaluate(ticker => {
     const link = document.createElement('a'); link.href = 'research.html?demo=1&ticker=' + ticker;
     document.body.append(link); link.click(); link.remove();
   }, ticker);
   assert.equal(await page.locator('#f-ticker').inputValue(), ticker);
   await page.locator('#back-link').click(); await page.waitForURL('**/more.html?demo=1');
 }
 assert.equal(calls.length, 0, 'demo must never read or write the API');

 // A cold open with failed reads must expose recovery controls, not an endless animation.
 const failing = await context.newPage();
 failing.on('pageerror', e => errors.push(e.message));
 let recover = false;
 await failing.route('**/*', async route => {
   const url = new URL(route.request().url());
   if (url.pathname.startsWith('/ui/')) return route.continue();
   if (url.pathname === '/favicon.ico') return route.fulfill({status:204});
   if (!recover) return route.abort('failed');
   return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(payload[url.pathname] || {})});
 });
 await failing.goto(origin + '/ui/index.html');
 await failing.waitForSelector('#startup-actions:not([hidden])');
 assert.equal(await failing.locator('#startup').isVisible(), true);
 assert.equal(await failing.locator('#content').isVisible(), false);
 recover = true;
 await failing.locator('#startup-retry').click();
 await failing.waitForSelector('#startup', {state:'detached'});
 assert.equal(await failing.locator('#content').isVisible(), true);
 await failing.close();

 // A direct secondary-page entry uses the same startup and has an instant parent fallback.
 const direct = await context.newPage();
 direct.on('pageerror', e => errors.push(e.message));
 await direct.route('**/*', route => {
   const url = new URL(route.request().url());
   if (url.pathname.startsWith('/ui/')) return route.continue();
   return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(payload[url.pathname] || {})});
 });
 await direct.goto(origin + '/ui/risk.html');
 await direct.waitForSelector('#startup', {state:'detached'});
 await direct.evaluate(() => window.coldEntryToken = 'same-document');
 await direct.locator('#back-link').click(); await direct.waitForURL('**/more.html');
 assert.equal(await direct.evaluate(() => window.coldEntryToken), 'same-document');
 assert.equal(await direct.locator('#content').isVisible(), true);
 assert.equal(await direct.locator('#startup, .page-skeleton').count(), 0);
 await direct.close();

 // Reduced motion is a static pair of blades, and a frozen request has a bounded wait.
 const reduced = await browser.newContext({viewport:{width:390,height:844}, reducedMotion:'reduce'});
 const stalled = await reduced.newPage();
 stalled.on('pageerror', e => errors.push(e.message));
 await stalled.clock.install();
 await stalled.route('**/*', route => {
   const url = new URL(route.request().url());
   if (url.pathname.startsWith('/ui/')) return route.continue();
   // Deliberately leave API routes unanswered. Advancing the clock drives the watchdog.
 });
 await stalled.goto(origin + '/ui/index.html');
 assert.equal(await stalled.locator('.lightsaber').first().evaluate(el => getComputedStyle(el).animationName), 'none');
 await stalled.clock.fastForward(10001);
 assert.equal(await stalled.locator('#startup-actions').isVisible(), true);
 await stalled.locator('#startup-continue').click();
 assert.equal(await stalled.locator('.tabbar').isVisible(), true);
 await reduced.close();
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({times,calls,errors},null,2));
 } finally {
   await browser?.close();
   await new Promise(resolve => server.close(resolve));
 }
})().catch(e=>{console.error(e);process.exit(1)});
