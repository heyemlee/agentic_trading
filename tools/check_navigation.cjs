// Offline browser regression: node tools/check_navigation.cjs (requires Playwright + Chrome).
const { chromium, webkit } = require('playwright');
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
                               'evaluation', 'profile', 'notifications', 'system', 'approvals', 'lab', 'settings')}))
`], {cwd: root, encoding:'utf8', maxBuffer: 8 * 1024 * 1024}));
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
 const safari = process.env.CT_BROWSER_ENGINE === 'webkit';
 browser = safari ? await webkit.launch({headless:true})
   : await chromium.launch({channel:process.env.CT_BROWSER_CHANNEL || 'chrome',headless:true});
 const context = await browser.newContext({viewport:{width:390,height:844},isMobile:true,hasTouch:true});
 const page = await context.newPage();
 const selectTab = async tab => {
   if (await page.locator('body').evaluate(el => el.classList.contains('advisor-page'))) {
     await page.locator('#assistant-back').click();
     await page.waitForFunction(() => !document.body.classList.contains('advisor-page'));
   }
   await page.locator(tab === 'advisor' ? '#assistant-launcher' : '[data-od-id="tab-' + tab + '"]').click();
 };
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
 '/advisor/conversations/default':{conversation_id:'default',messages:[]},
 '/research/jobs':[], '/research':[],
 };
 const fixtureContext = { URLSearchParams, window: { location: { search: '?demo=1' } } };
 require('node:vm').runInNewContext(await fs.readFile(path.join(root, 'ui/assets/data.js'), 'utf8'), fixtureContext);
 for (const [key, endpoint] of Object.entries({risk:'/risk', microLive:'/micro-live',
   schedule:'/system/schedule', operations:'/system/operations', notifications:'/system/notifications',
   authSessions:'/auth/sessions', pushDevices:'/notifications/devices', evaluation:'/evaluation', activity:'/activity'})) {
   payload[endpoint] = fixtureContext.window.CT[key];
 }
 const apiPayload = url => url.pathname === '/evaluation'
   ? fixtureContext.window.CT.evaluationExamples[url.searchParams.get('scope') || 'demo']
   : url.pathname === '/advisor' && url.searchParams.has('scope')
     ? { ...payload['/advisor'], scope: 'personal', account_scope_kind: 'all_personal', account_id: null, conversation_summaries: [] }
   : payload[url.pathname] || {};
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
  await route.fulfill({status:denied?401:200,contentType:'application/json',body:JSON.stringify(denied?{detail:'login required'}:apiPayload(url))});
 });
 await page.goto(origin + '/ui/index.html', {waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>window.CT_NAV && !document.getElementById('content').hidden);
 assert.equal(await page.locator('.page-skeleton').count(),0);
 assert.equal(await page.locator('#startup').isVisible(),true);
 assert.equal(await page.locator('#startup').evaluate(el => el.classList.contains('is-first')), true);
 assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).backgroundColor), 'rgb(255, 255, 255)');
 assert.equal(await page.evaluate(() => getComputedStyle(document.body).backgroundColor), 'rgb(255, 255, 255)');
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
    document.querySelector(tab === 'advisor' ? '#assistant-launcher' : '[data-od-id="tab-'+tab+'"]').click();
    return {ms:performance.now()-before,path:location.pathname,hidden:document.getElementById('content').hidden};
   },tab);
   times.push(t); assert.equal(t.hidden,false);assert.ok(t.ms<100);
   assert.ok(t.path.endsWith((tab==='dashboard'?'index':tab)+'.html'));
 }
 await page.waitForTimeout(2400);
 await page.waitForSelector('#startup', {state:'detached'});
 assert.equal(await page.locator('.tabbar').isVisible(),false);
 assert.equal(await page.locator('.tabitem:focus').count(), 0, 'startup must not leave a focus ring on the selected tab');
 if (!safari) assert.equal(await page.locator('#content').evaluate(el => el === document.activeElement), true);
 assert.equal(await page.locator('#assistant-back').isVisible(), true);
 await page.locator('#chat-input').fill('Draft stays across tabs');
 await selectTab('portfolio');
 // iOS keeps a scrolled page's visual offset while the next view changes the
 // shared body to fixed positioning. Entering chat must clear it first.
 const previousScroll = await page.evaluate(() => {
   const spacer = document.createElement('div');
   spacer.id = 'navigation-scroll-fixture'; spacer.style.height = '1200px';
   document.getElementById('content').append(spacer);
   window.scrollTo(0, 480);
   window.navigationScrollOrder = [];
   window.navigationOriginalScrollTo = window.scrollTo;
   window.scrollTo = function (...args) {
     navigationScrollOrder.push({fixed:document.body.classList.contains('chat-view'), top:args.at(-1)});
     return navigationOriginalScrollTo.apply(this, args);
   };
   return window.scrollY;
 });
 assert.ok(previousScroll > 0, 'Navigation regression needs a scrolled source page');
 await selectTab('advisor');
 assert.equal(await page.locator('#chat-input').inputValue(),'Draft stays across tabs');
 const enteredChat = await page.evaluate(() => {
   const body = document.body.getBoundingClientRect();
   const dock = document.querySelector('.composer-dock').getBoundingClientRect();
   return {scrollY, bodyTop: body.top, bodyBottom: body.bottom, dockBottom: dock.bottom, innerHeight,
     bodyPosition: getComputedStyle(document.body).position};
 });
 assert.equal(enteredChat.scrollY, 0);
 assert.equal(enteredChat.bodyPosition, 'relative',
   'Normal chat must not fix the whole body; real iOS can retain a stale fixed-body viewport');
 const scrollOrder = await page.evaluate(() => {
   const result = navigationScrollOrder;
   window.scrollTo = navigationOriginalScrollTo;
   delete window.navigationOriginalScrollTo;
   delete window.navigationScrollOrder;
   return result;
 });
 assert.equal(scrollOrder[0]?.fixed, false, 'Source scroll must clear before chat becomes fixed');
 assert.ok(Math.abs(enteredChat.bodyTop) < 2 && Math.abs(enteredChat.bodyBottom - enteredChat.innerHeight) < 2,
   'Chat shell must fill the viewport after bottom-tab navigation: ' + JSON.stringify(enteredChat));
 assert.ok(Math.abs(enteredChat.dockBottom - enteredChat.innerHeight) < 2,
   'Composer fills the space previously reserved for navigation: ' + JSON.stringify(enteredChat));
 assert.equal(await page.locator('.tabbar').isVisible(), false);
 assert.equal(await page.locator('#assistant-back').isVisible(), true);
 // Route transitions can leave a short visual viewport even with no focused
 // editor. Browser chrome alone must not hide the tab bar as if a keyboard opened.
 await page.evaluate(() => {
   Object.defineProperty(visualViewport, 'height', {configurable:true, value:innerHeight - 180});
   visualViewport.dispatchEvent(new Event('resize'));
 });
 await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
 assert.equal(await page.locator('.tabbar').isVisible(), false);
 assert.equal(await page.evaluate(() => document.body.classList.contains('assistant-keyboard')), false);
 await page.evaluate(() => { delete visualViewport.height; visualViewport.dispatchEvent(new Event('resize')); });
 if (process.env.CT_UI_CAPTURE_DIR) {
   const folder = path.resolve(process.env.CT_UI_CAPTURE_DIR);
   await fs.mkdir(folder, {recursive:true});
   await page.screenshot({path:path.join(folder, 'assistant-after-tab-navigation.png'), animations:'disabled'});
 }
 // Account changes are part of one Assistant visit: Back exits to the source.
 await page.evaluate(() => {
   const link = document.createElement('a'); link.href = 'advisor.html?scope=personal';
   document.body.append(link); link.click(); link.remove();
 });
 await page.locator('#assistant-back').click();
 await page.waitForURL('**/portfolio.html');
 assert.equal(await page.locator('.tabbar').isVisible(), true);
 await selectTab('advisor');
 // Leave while the keyboard is open; close it on another page, then return
 // without a viewport resize event. A retained chat must not keep a short body.
 await page.evaluate(() => {
   document.getElementById('chat-input').focus();
   Object.defineProperty(visualViewport, 'height', { configurable: true, value: 430 });
   Object.defineProperty(visualViewport, 'offsetTop', { configurable: true, value: 120 });
   visualViewport.dispatchEvent(new Event('resize'));
 });
 await page.waitForFunction(() => document.body.classList.contains('assistant-keyboard'));
 await page.locator('#assistant-back').click();
 await page.waitForURL('**/portfolio.html');
 assert.equal(await page.locator('.tabbar').isVisible(), true);
 assert.equal(await page.evaluate(() => getComputedStyle(document.body).position), 'static');
 await page.evaluate(() => { delete visualViewport.height; delete visualViewport.offsetTop; });
 await selectTab('advisor');
 await page.waitForFunction(() => !document.body.classList.contains('assistant-keyboard') &&
   Math.abs(document.body.getBoundingClientRect().height - visualViewport.height) < 2);
 assert.equal(await page.locator('#chat-input').inputValue(),'Draft stays across tabs');
 assert.ok(await page.evaluate(() => Math.abs(document.querySelector('.composer-dock').getBoundingClientRect().bottom -
   innerHeight) < 2));
 await page.locator('#view-discover').click();
 await selectTab('more');
 await selectTab('advisor');
 assert.equal(await page.locator('#view-discover').getAttribute('aria-selected'),'true');
 await page.goBack();assert.ok(page.url().endsWith('more.html'));
 await page.goForward();assert.ok(page.url().endsWith('advisor.html'));
 assert.equal(await page.locator('.topnav').count(),1);
 assert.equal(await page.locator('.tabbar').count(),1);
 assert.equal(await page.locator('#content').count(),1);
 assert.equal(await page.locator('#startup').count(),0, 'route changes must not replay the startup');
 await selectTab('portfolio');
 await selectTab('dashboard');

 // Assistant is a header action; the Lab occupies the third tab. Returning from
 // Assistant must restore the Lab, including its selected navigation state.
 assert.deepEqual(await page.locator('.tabbar .tabitem').evaluateAll(tabs => tabs.map(el => el.getAttribute('href'))),
   ['index.html', 'portfolio.html', 'lab.html', 'more.html']);
 assert.equal(await page.locator('.tabbar a[href="advisor.html"]').count(), 0);
 await selectTab('lab');
 await page.waitForFunction(() => !document.getElementById('content').inert);
 assert.equal(await page.locator('[data-od-id="tab-lab"]').getAttribute('aria-current'), 'page');
 await page.locator('#assistant-launcher').click();
 await page.waitForURL('**/advisor.html');
 assert.equal(await page.locator('#assistant-back').isVisible(), true);
 await page.locator('#assistant-back').click();
 await page.waitForURL('**/lab.html');
 assert.equal(await page.locator('[data-od-id="tab-lab"]').getAttribute('aria-current'), 'page');
 await page.emulateMedia({reducedMotion:'reduce'});
 assert.equal(await page.locator('#assistant-launcher').evaluate(el => getComputedStyle(el, '::before').animationName), 'none');
 await page.emulateMedia({reducedMotion:'no-preference'});
 assert.equal(await page.locator('#assistant-launcher').evaluate(el => getComputedStyle(el, '::before').animationName), 'assistant-halo');
 await selectTab('dashboard');

 // One lens survives routing. Tap transitions pass through intermediate
 // positions; dragging previews continuously and commits just once on release.
 const lensAligned = () => page.waitForFunction(() => {
   const lens = document.querySelector('.tab-indicator').getBoundingClientRect();
   const tab = document.querySelector('.tabitem[aria-current], .tabitem[data-section-active]').getBoundingClientRect();
   return Math.abs(lens.left - tab.left) < 1 && Math.abs(lens.width - tab.width) < 1;
 });
 await lensAligned();
 const slide = await page.evaluate(async () => {
   const lens = document.querySelector('.tab-indicator');
   window.navigationLens = lens;
   const from = lens.getBoundingClientRect().left;
   document.querySelector('[data-od-id="tab-more"]').click();
   await new Promise(resolve => setTimeout(resolve, 100));
   return {same:lens === document.querySelector('.tab-indicator'), from,
     middle:lens.getBoundingClientRect().left,
     to:document.querySelector('[data-od-id="tab-more"]').getBoundingClientRect().left};
 });
 assert.equal(slide.same, true, 'Routing preserves the animated lens');
 assert.ok(slide.middle > slide.from + 1 && slide.middle < slide.to - 1, 'Selection slides rather than jumping');
 await selectTab('portfolio'); // Reverse while the previous animation is in flight.
 await lensAligned();
 assert.equal(await page.evaluate(() => window.navigationLens === document.querySelector('.tab-indicator')), true);
 const portfolioTab = await page.locator('[data-od-id="tab-portfolio"]').boundingBox();
 const labTab = await page.locator('[data-od-id="tab-lab"]').boundingBox();
 const cx = box => box.x + box.width / 2;
 const cy = portfolioTab.y + portfolioTab.height / 2;
 const depthBeforeDrag = await page.evaluate(() => history.state.ctDepth);
 await page.mouse.move(cx(portfolioTab), cy);
 await page.mouse.down();
 await page.mouse.move((cx(portfolioTab) + cx(labTab)) / 2, cy, {steps:4});
 assert.ok(page.url().endsWith('portfolio.html'), 'A drag does not navigate before release');
 const dragLeft = await page.locator('.tab-indicator').evaluate(el => el.getBoundingClientRect().left);
 assert.ok(dragLeft > portfolioTab.x + 5 && dragLeft < labTab.x - 5, 'Lens tracks the finger between tabs');
 await page.mouse.move(cx(labTab), cy, {steps:4});
 await page.mouse.up();
 await page.waitForURL('**/lab.html');
 await lensAligned();
 assert.equal(await page.evaluate(() => history.state.ctDepth), depthBeforeDrag + 1, 'Drag commits one route');
 await page.goBack();
 await page.waitForURL('**/portfolio.html');
 await lensAligned();
 // Exercise native touch panning as well as mouse pointer capture in Chrome.
 if (!safari) {
   const cdp = await context.newCDPSession(page);
   await cdp.send('Input.dispatchTouchEvent', {type:'touchStart', touchPoints:[{x:cx(portfolioTab),y:cy}]});
   for (let step = 1; step <= 6; step++) {
     await cdp.send('Input.dispatchTouchEvent', {type:'touchMove',
       touchPoints:[{x:cx(portfolioTab) + (cx(labTab) - cx(portfolioTab)) * step / 6,y:cy}]});
   }
   assert.ok(page.url().endsWith('portfolio.html'));
   await cdp.send('Input.dispatchTouchEvent', {type:'touchEnd', touchPoints:[]});
   await page.waitForURL('**/lab.html');
   await lensAligned();
   await page.goBack();
   await page.waitForURL('**/portfolio.html');
   await lensAligned();
   await cdp.detach();
 }
 // Pointer cancellation restores the current section without committing a route.
 await page.mouse.move(cx(portfolioTab), cy);
 await page.mouse.down();
 await page.mouse.move(cx(labTab), cy, {steps:4});
 await page.locator('.tabbar').dispatchEvent('pointercancel', {pointerId:1});
 await page.mouse.up();
 await lensAligned();
 assert.ok(page.url().endsWith('portfolio.html'));
 await page.emulateMedia({reducedMotion:'reduce'});
 await selectTab('lab');
 await lensAligned();
 assert.equal(await page.locator('.tab-indicator').evaluate(el => getComputedStyle(el).transitionDuration), '0s');
 await page.emulateMedia({reducedMotion:'no-preference'});
 await selectTab('advisor');
 await page.locator('#assistant-back').click();
 await page.waitForURL('**/lab.html');
 await lensAligned();

 // More tools must switch synchronously even while their first reads are pending.
 await selectTab('more');
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
   assert.equal(await page.locator('.topnav #nav-status, #resource-status').count(), 0);
   assert.equal(await page.locator('#conn-bar').isVisible(), false);
   if (name === 'system') {
     await page.waitForFunction(() => CT_API.isLive('health'));
     await page.evaluate(() => {
       CT_API.state.resources.researchJobs = { lastOkAt: 1, error: 'Diagnostic test: stale research jobs' };
       document.dispatchEvent(new Event('ct:connection'));
     });
     assert.equal(await page.locator('#conn-bar').isVisible(), false, 'Stale-data details never appear above the page');
     assert.equal(await page.locator('#sources-sheet').isVisible(), false);
     await page.locator('#diagnostics > summary').click();
     await page.locator('#diagnostics [data-open-sheet="sources-sheet"]').click();
     assert.equal(await page.locator('#sources-sheet #nav-status').isVisible(), true);
     assert.equal(await page.locator('#sources-sheet #conn-bar').isVisible(), true);
     assert.match(await page.locator('#source-rows').innerText(), /Diagnostic test: stale research jobs/);
     if (process.env.CT_UI_CAPTURE_DIR) {
       await page.screenshot({path:path.join(path.resolve(process.env.CT_UI_CAPTURE_DIR), 'system-data-details.png'), animations:'disabled'});
     }
     await page.locator('[data-close-sheet="sources-sheet"]').click();
     await page.waitForFunction(() => !document.getElementById('sources-sheet').open);
     await page.locator('#diagnostics > summary').click();
     if (process.env.CT_UI_CAPTURE_DIR) {
       await page.screenshot({path:path.join(path.resolve(process.env.CT_UI_CAPTURE_DIR), 'system-clean-header.png'), animations:'disabled'});
     }
   }
   if (name === 'risk') {
     assert.equal(await page.locator('#page-loader').isVisible(), true);
     assert.equal(await page.locator('#page-loader .lightsaber').count(), 2);
     assert.equal((await page.locator('#page-loader').innerText()).trim(), '');
     assert.equal(await page.locator('.startup-brand').count(), 0);
   }
   await page.locator('#back-link').click();
   await page.waitForURL('**/more.html');
   assert.equal(await page.evaluate(() => window.navigationTestToken), documentToken);
   assert.equal(await page.locator('#content').isVisible(), true);
   assert.equal(await page.locator('#page-loader').isVisible(), false);
   assert.equal(await page.locator('#startup, .page-skeleton').count(), 0);
 }
 // Floating icon navigation stays reachable at small widths and above the
 // home indicator, with translated accessible names despite hidden labels.
 for (const [width, height] of [[320,568], [390,844], [440,956], [1280,900]]) {
   await page.setViewportSize({width, height});
   await lensAligned();
   await page.evaluate(() => document.documentElement.style.setProperty('--tabbar-bottom', '34px'));
   const layout = await page.evaluate(() => ({
     bar: document.querySelector('.tabbar').getBoundingClientRect().toJSON(),
     padding: parseFloat(getComputedStyle(document.body).paddingBottom),
     tabs: [...document.querySelectorAll('.tabitem')].map(el => ({
       rect: el.getBoundingClientRect().toJSON(),
       label: el.querySelector('[data-nav-label]').textContent,
       labelWidth: el.querySelector('[data-nav-label]').getBoundingClientRect().width,
     })),
   }));
   assert.equal(layout.bar.height, 60);
   assert.equal(layout.bar.bottom, height - 34);
   assert.ok(layout.bar.left >= 16 && layout.bar.right <= width - 16);
   assert.ok(Math.abs(layout.bar.left - (width - layout.bar.right)) < 1, 'Glass capsule stays centered');
   assert.ok(layout.padding >= layout.bar.height + 34 + 16, 'Page content can scroll clear of the floating bar');
   const launcher = await page.locator('#assistant-launcher').boundingBox();
   assert.ok(launcher.width >= 44 && launcher.height >= 44 && launcher.x + launcher.width <= width - 8,
     'The header assistant remains a reachable, unclipped touch target');
   for (const tab of layout.tabs) {
     assert.ok(tab.rect.width >= 44 && tab.rect.height >= 44);
     assert.ok(Math.abs(tab.rect.width - layout.tabs[0].rect.width) < 1, 'Icons have even spacing');
     assert.equal(tab.labelWidth, 1, 'Labels are clipped, not visually displayed');
     assert.equal(await page.locator('.tabbar').getByRole('link', {name:tab.label, exact:true}).count(), 1);
   }
   if (process.env.CT_UI_CAPTURE_DIR) {
     await page.screenshot({path:path.join(path.resolve(process.env.CT_UI_CAPTURE_DIR), 'glass-navigation-' + width + '.png'), animations:'disabled'});
   }
 }
 await page.evaluate(() => document.documentElement.style.removeProperty('--tabbar-bottom'));
 await page.setViewportSize({width:390, height:844});
 await page.locator('#content a[href="profile.html"]').click();
 await page.locator('#profile-notes').fill('Personal draft survives returning to More');
 await page.locator('#back-link').click(); await page.waitForURL('**/more.html');
 await page.goForward(); await page.waitForURL('**/profile.html');
 assert.equal(await page.locator('#profile-notes').inputValue(), 'Personal draft survives returning to More');
 await selectTab('dashboard');

 assert.equal(calls.filter(c=>c.path==='/auth/session').length,1);
 assert.equal(calls.filter(c=>c.method==='POST').length,0);
 assert.equal(await page.evaluate(()=>performance.getEntriesByType('navigation').length),1);
 delay=0;denied=true;
 await page.evaluate(()=>CT_API.readOnce('portfolio'));
 assert.equal(await page.locator('#content').isVisible(),false);
 await selectTab('advisor');
 assert.equal(await page.locator('#content').isVisible(),false);
 assert.equal(await page.locator('#chat-input').inputValue(),'');
 await selectTab('more');
 await page.evaluate(() => document.querySelector('#content a[href="profile.html"]').click());
 assert.equal(await page.locator('#profile-notes').inputValue(), '');
 assert.equal(await page.locator('#content').isVisible(), false);
 calls.length = 0;
 await page.goto(origin + '/ui/index.html?demo=1');
 for (const tab of ['advisor', 'portfolio', 'more', 'dashboard']) {
   await selectTab(tab);
   assert.ok(page.url().includes('demo=1'));
   assert.equal(await page.locator('#content').isVisible(), true);
 }
 for (const name of ['risk', 'activity', 'evaluation', 'profile', 'notifications', 'system', 'approvals', 'lab']) {
   await page.evaluate(name => {
     const link = document.createElement('a'); link.href = name + '.html?demo=1';
     document.body.append(link); link.click(); link.remove();
   }, name);
   await page.waitForFunction(() => !document.getElementById('content').inert);
   assert.ok(page.url().includes(name + '.html?demo=1'));
   assert.equal(await page.locator('#content').isVisible(), true);
   assert.equal(await page.locator('#startup, .page-skeleton').count(), 0);
   assert.equal(await page.locator('.topnav').count(), 1);
   if (name === 'system') {
     await page.locator('#diagnostics').evaluate(details => { details.open = true; });
     assert.match(await page.locator('#sc-mode').innerText(), /Per account|按账户调度/);
     assert.equal(await page.locator('#sc-tasks .list-row').count(), 2);
     assert.match(await page.locator('#sc-tasks').innerText(), /Holdings review|持仓复查/);
     assert.match(await page.locator('#sc-tasks').innerText(), /Candidate research|候选研究/);
     assert.match(await page.locator('#op-state').innerText(), /2 need attention|2 项需处理/);
     assert.match(await page.locator('[data-od-id="op-maintenance"]').innerText(), /Awaiting first run|等待首次运行/);
     // Full diagnostic text, including its tail amount, survives a narrow screen.
     const reason = 'CashMismatch: ' + 'diagnostic context '.repeat(35) + '\n101.50 versus 100.00; difference 1.50 <untrusted>';
     await page.evaluate(reason => {
       CT.operations.activities.find(row => row.id === 'ledger_confirm').last_failure.reason = reason;
       document.dispatchEvent(new CustomEvent('ct:data'));
     }, reason);
     assert.equal(await page.locator('[data-od-id="op-ledger_confirm"] .operation-failure .operation-text').textContent(), reason);
     assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
     if (process.env.CT_UI_CAPTURE_DIR) {
       const folder = path.resolve(process.env.CT_UI_CAPTURE_DIR);
       await fs.mkdir(folder, {recursive:true});
       await page.locator('[data-od-id="schedule"]').scrollIntoViewIfNeeded();
       await page.screenshot({path:path.join(folder, 'schedule-tasks-phone.png')});
       await page.locator('[data-od-id="op-ledger_confirm"]').scrollIntoViewIfNeeded();
       await page.screenshot({path:path.join(folder, 'activity-states-phone.png')});
     }
   }
 }
 await selectTab('more');
 // Research lives in the Lab. A research link written before then opens the Lab, with the report
 // it names over the room; the Lab is a tab, so its own tab is the way back, as for Home and Assets.
 await page.evaluate(() => {
   const link = document.createElement('a'); link.href = 'research.html?demo=1&run=2026-09-04_VTI_873e64ae';
   document.body.append(link); link.click(); link.remove();
 });
 await page.waitForURL('**/lab.html?demo=1&run=2026-09-04_VTI_873e64ae');
 await page.waitForFunction(() => document.getElementById('trace-sheet').open);
 assert.equal(await page.locator('#back-link').count(), 0);
 assert.equal(await page.locator('[data-od-id="tab-lab"]').getAttribute('aria-current'), 'page');
 await page.locator('#trace-close').click();
 await selectTab('more'); await page.waitForURL('**/more.html?demo=1');
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
   return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(apiPayload(url))});
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
 let directDelay = 0;
 direct.on('pageerror', e => errors.push(e.message));
 await direct.route('**/*', async route => {
   const url = new URL(route.request().url());
   if (url.pathname.startsWith('/ui/')) return route.continue();
   if (directDelay) await new Promise(resolve => setTimeout(resolve, directDelay));
   return route.fulfill({status:200, contentType:'application/json', body:JSON.stringify(apiPayload(url))});
 });
 await direct.goto(origin + '/ui/advisor.html');
 await direct.waitForSelector('#startup', {state:'detached'});
 assert.equal(await direct.locator('.tabbar').isVisible(), false);
 await direct.locator('#assistant-back').click(); await direct.waitForURL('**/index.html');
 assert.equal(await direct.locator('.tabbar').isVisible(), true);
 await direct.goto(origin + '/ui/risk.html');
 await direct.waitForSelector('#startup', {state:'detached'});
 await direct.evaluate(() => window.coldEntryToken = 'same-document');
 await direct.locator('#back-link').click(); await direct.waitForURL('**/more.html');
 assert.equal(await direct.evaluate(() => window.coldEntryToken), 'same-document');
 assert.equal(await direct.locator('#content').isVisible(), true);
 assert.equal(await direct.locator('#startup, .page-skeleton').count(), 0);
 directDelay = 1200;
 await direct.goto(origin + '/ui/evaluation.html', {waitUntil:'domcontentloaded'});
 assert.equal(await direct.locator('#startup').isVisible(), true);
 assert.equal(await direct.locator('#startup .startup-brand').isVisible(), false);
 assert.equal(await direct.locator('#startup .startup-caption').isVisible(), false);
 assert.equal(await direct.locator('#startup .lightsaber').count(), 2);
 assert.equal(await direct.locator('#startup-message').evaluate(el => getComputedStyle(el).clipPath), 'inset(50%)');
 if (process.env.CT_UI_CAPTURE_DIR) {
   await direct.screenshot({path:path.join(path.resolve(process.env.CT_UI_CAPTURE_DIR), 'internal-loading-phone.png')});
 }
 await direct.waitForSelector('#startup', {state:'detached'});
 await direct.reload({waitUntil:'domcontentloaded'});
 assert.equal(await direct.locator('#startup .startup-brand').isVisible(), false);
 await direct.waitForSelector('#startup', {state:'detached'});
 await direct.close();

 // The standalone login page initializes the shared office factory itself.
 const login = await context.newPage();
 login.on('pageerror', e => errors.push(e.message));
 await login.goto(origin + '/ui/login.html');
 await login.waitForFunction(() => window.labPixelScene && document.querySelector('#pixel-office'));
 await login.waitForFunction(() => {
   const canvas = document.querySelector('#pixel-office');
   const pixels = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
   const first = pixels.slice(0, 4).join(',');
   for (let i = 4; i < pixels.length; i += 4) {
     if (pixels.slice(i, i + 4).join(',') !== first) return true;
   }
   return false;
 });
 assert.equal(await login.locator('#scene-caption').count(), 0);
 assert.ok(await login.locator('#pixel-office').evaluate(canvas => {
   const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
   const colours = new Set();
   for (let i = 0; i < data.length; i += 64) colours.add(data.slice(i, i + 3).join(','));
   return colours.size > 12;
 }), 'login illustration must contain the agents and room, not one flat background');
 if (process.env.CT_UI_CAPTURE_DIR) {
   await login.screenshot({path:path.join(path.resolve(process.env.CT_UI_CAPTURE_DIR), 'login-phone.png')});
 }
 await login.close();

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
