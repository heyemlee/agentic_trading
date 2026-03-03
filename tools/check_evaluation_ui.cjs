// Offline evaluation check: static UI only; all API traffic uses synthetic responses.
// Playwright + Chrome; optional CT_UI_CAPTURE_DIR for screenshots.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const vm = require('node:vm');
const ui = path.resolve(__dirname, '../ui');

(async () => {
  const fixture = { URLSearchParams, window: { location: { search: '?demo=1' } } };
  vm.runInNewContext(await fs.readFile(path.join(ui, 'assets/data.js'), 'utf8'), fixture);
  const data = fixture.window.CT, now = new Date().toISOString();
  const reports = structuredClone(data.evaluationExamples);
  reports.demo.sample = { basis: 'sessions', last_snapshot_on: '2026-09-10', stale: true,
    sessions_behind: 6, gaps: [{ from: '2026-09-01', to: '2026-09-04', missing_sessions: 2, reason: 'no snapshot' }] };
  reports.route_quality.checked_at = now;
  const payload = {
    '/auth/session': { authenticated: true, username: 'preview', csrf_token: 'test-only' },
    '/health': { ...data.health, checked_at: now }, '/portfolio': { ...data.portfolio, checked_at: now },
    '/advisor': data.advisor, '/proposals': [],
  };
  const errors = []; let delayDemo = false, heldDemo;
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/ui/')) { res.writeHead(404).end(); return; }
    const file = path.resolve(ui, '.' + url.pathname.slice(3));
    if (!file.startsWith(ui + path.sep)) { res.writeHead(403).end(); return; }
    try {
      const body = await fs.readFile(file);
      const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
      res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream' }).end(body);
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser, page;
  try {
    browser = await chromium.launch({ channel: process.env.CT_BROWSER_CHANNEL || 'chrome', headless: true });
    page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => sessionStorage.setItem('ct.startup.seen.v1', '1'));
    const origin = 'http://127.0.0.1:' + server.address().port;
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'the preview must never reach an external service');
      if (url.pathname.startsWith('/ui/')) return route.continue();
      assert.equal(request.method(), 'GET', 'evaluation must never write');
      if (url.pathname === '/evaluation' && url.searchParams.get('scope') === 'demo' && delayDemo) {
        heldDemo = route; return;
      }
      const body = url.pathname === '/evaluation' ? reports[url.searchParams.get('scope')] : payload[url.pathname] || {};
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    const screenshot = async name => {
      if (!process.env.CT_UI_CAPTURE_DIR) return;
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name) });
    };
    await page.goto(origin + '/ui/evaluation.html');
    await page.waitForFunction(() => document.getElementById('ev-not-started').hidden === false);
    await page.evaluate(() => I18N.set('zh'));
    assert.equal(await page.locator('#ev-performance').isVisible(), false);
    assert.match(await page.locator('#ev-state').innerText(), /尚未开始/);
    const select = scope => page.locator('#evaluation-scopes a[href="evaluation.html?scope=' + scope + '"]').click();
    await select('demo');
    await page.waitForFunction(() => document.getElementById('ev-sample-status').textContent.includes('落后超过'));
    assert.match(await page.locator('#ev-gaps').innerText(), /缺少 2 个交易日/);
    assert.equal(await page.locator('#ev-performance').isVisible(), true);
    await screenshot('evaluation-demo-phone.png');
    await select('route_quality');
    await page.waitForFunction(() => document.getElementById('evaluation-routes').hidden === false);
    assert.match(await page.locator('#ev-routes').innerText(), /事实准确率\n未评估/);
    assert.equal(await page.locator('#evaluation-report').isVisible(), false);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await screenshot('evaluation-quality-phone.png');
    await page.goBack();
    await page.waitForFunction(() => window.CT.evaluation?.scope === 'demo');
    assert.equal(await page.locator('#evaluation-scopes [aria-current]').innerText(), 'Demo');
    await select('agentic');
    await page.waitForFunction(() => window.CT.evaluation?.scope === 'agentic');
    delayDemo = true;
    const demoRequested = page.waitForRequest(request => request.url().endsWith('/evaluation?scope=demo'));
    await select('demo'); await demoRequested;
    await select('route_quality');
    assert.equal(await page.locator('#evaluation-report').isVisible(), false);
    assert.equal(await page.locator('#evaluation-routes').isVisible(), false);
    assert.ok(heldDemo);
    await heldDemo.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reports.demo) });
    await page.waitForFunction(() => window.CT.evaluation?.scope === 'route_quality');
    assert.equal(await page.locator('#evaluation-report').isVisible(), false);
    assert.equal(await page.locator('#evaluation-scopes [aria-current]').innerText(), '研究质量');
    assert.deepEqual(errors, []);
    console.log('Evaluation browser check passed: three scopes, NOT_STARTED, sample gaps, unmeasured quality, history, late response isolation, mobile layout.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ page: location.pathname, text: document.body.innerText.slice(-3000),
      resources: window.CT_API?.state.resources })));
    console.error(errors); throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
