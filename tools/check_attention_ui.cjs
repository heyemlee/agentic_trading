// Offline mobile check: static UI only; all API traffic uses synthetic responses.
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
  const run = '20260918_REVIEW_attention';
  const job = { run_key: run, state: 'COMPLETE', created_at: now, opened_at: null,
    assistant_result: { answer: 'Synthetic portfolio review: concentration needs another look.' } };
  const item = { id: 'personal_task_done:' + run, kind: 'personal_task_done', priority: 5,
    severity: 'info', requires_person: false, subject: run, since: now, account_label: 'Personal account',
    title: 'Portfolio review finished', detail: 'A result is ready to read.', link: { page: 'advisor', params: { scope: 'personal' } } };
  const attention = { checked_at: now, readable: true, unreadable: [], items: [item], top: item };
  const expired = { ...data.proposalDetail, proposal_id: 'expired-attention', status: 'EXPIRED', created_at: now };
  const payload = {
    '/auth/session': { authenticated: true, username: 'preview', csrf_token: 'test-only' },
    '/health': { ...data.health, checked_at: now }, '/portfolio': { ...data.portfolio, checked_at: now },
    '/micro-live': { ...data.microLive, checked_at: now }, '/advisor': data.advisor,
    '/proposals': [], ['/proposals/' + expired.proposal_id]: expired,
    '/config/authority': { version: 'preview', checked_at: now, actions: {} },
    '/attention': attention, '/research': [], '/research/jobs': [job], '/orders': { fills: [], tracked_orders: [] },
    ['/research/' + run]: job, ['/research/' + run + '/trace']: { run_id: run, steps: [] },
  };
  const posts = [], errors = [];
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
      let body = payload[url.pathname] || {};
      if (request.method() === 'POST') {
        assert.equal(url.pathname, '/research/' + run + '/opened');
        assert.deepEqual(request.postDataJSON(), {});
        assert.equal(request.headers()['x-csrf-token'], 'test-only');
        assert.equal(await page.locator('#trace-steps').isVisible(), true);
        assert.match(await page.locator('#trace-steps').innerText(), /Synthetic portfolio review/);
        posts.push(url.pathname); job.opened_at = new Date().toISOString();
        attention.items = []; attention.top = null; body = job;
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    const screenshot = async name => {
      if (!process.env.CT_UI_CAPTURE_DIR) return;
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name) });
    };
    await page.goto(origin + '/ui/index.html');
    await page.waitForFunction(() => document.querySelector('[data-attention-id]'));
    await page.evaluate(() => I18N.set('zh'));
    await page.locator('[data-attention-id]').scrollIntoViewIfNeeded();
    assert.equal(posts.length, 0, 'listing a completed job must not mark it opened');
    assert.equal(await page.locator('[data-attention-id] a').getAttribute('href'), 'research.html?run=' + run);
    await screenshot('attention-phone.png');
    await page.locator('[data-attention-id] a').click();
    await page.waitForFunction(() => window.CT.attention?.items.length === 0);
    assert.equal(posts.length, 1);
    assert.equal(await page.locator('#trace-sheet').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    await screenshot('attention-result-phone.png');
    await page.locator('#trace-close').click();
    await page.goBack();
    await page.waitForFunction(() => document.querySelector('[data-od-id="empty-queue"]'));
    assert.equal(await page.locator('[data-attention-id]').count(), 0);
    attention.readable = false; attention.unreadable = ['Synthetic unavailable source'];
    await page.evaluate(() => CT_API.readOnce('attention'));
    assert.equal(await page.locator('[data-od-id="empty-queue"]').count(), 0);
    assert.match(await page.locator('#needs-you-slot').innerText(), /无法确认/);
    await page.goto(origin + '/ui/approvals.html?proposal=' + expired.proposal_id);
    await page.waitForFunction(() => document.getElementById('d-id').textContent === 'expired-attention');
    assert.equal(await page.locator('#btn-approve').isDisabled(), true);
    assert.equal(posts.length, 1, 'navigation and subsequent polls must not repeat the receipt');
    assert.deepEqual(errors, []);
    console.log('Attention browser check passed: exact result, visible-only receipt, resolved item disappears, partial state, expired proposal, mobile layout.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ page: location.pathname, text: document.body.innerText.slice(-3000),
      resources: window.CT_API?.state.resources })));
    console.error(errors); throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
