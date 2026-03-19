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
    '/health': { ...data.health, warnings: [], checked_at: now }, '/portfolio': { ...data.portfolio, checked_at: now },
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
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name), animations: 'disabled' });
    };
    await page.goto(origin + '/ui/index.html');
    await page.waitForFunction(() => document.querySelector('[data-attention-id]'));
    await page.evaluate(() => I18N.set('zh'));
    await page.locator('[data-attention-id]').scrollIntoViewIfNeeded();
    assert.equal(posts.length, 0, 'listing a completed job must not mark it opened');
    assert.equal(await page.locator('[data-attention-id] a').getAttribute('href'), 'lab.html?run=' + run);
    await screenshot('attention-phone.png');
    await page.locator('[data-attention-id] a').click();
    await page.waitForFunction(() => window.CT.attention?.items.length === 0);
    assert.equal(posts.length, 1);
    assert.equal(await page.locator('#trace-sheet').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    await screenshot('attention-result-phone.png');
    await page.locator('#trace-close').click();
    await page.goBack();
    await page.waitForFunction(() => document.getElementById('home-attention')?.hidden);
    assert.equal(await page.locator('[data-attention-id]').count(), 0);
    await screenshot('attention-empty-phone.png');
    attention.readable = false; attention.unreadable = ['Synthetic unavailable source'];
    await page.evaluate(() => CT_API.readOnce('attention'));
    assert.equal(await page.locator('[data-od-id="empty-queue"]').count(), 0);
    assert.equal(await page.locator('#home-attention').isVisible(), true);
    assert.match(await page.locator('#needs-you-slot').innerText(), /无法确认/);
    await screenshot('attention-unavailable-phone.png');

    // A collapsed deck never buries the highest priority item. Disclosure works
    // by keyboard/touch, and a new poll preserves the user's expanded view.
    attention.readable = true; attention.unreadable = [];
    attention.items = [
      { ...item, id: 'order', kind: 'order_unknown', severity: 'critical', requires_person: true,
        title: '订单状态待核对', detail: '券商尚未返回最终成交状态，请核对后继续。',
        link: { page: 'activity', params: { subject: 'order-preview' } } },
      { ...item, id: 'proposal', kind: 'proposal_waiting', severity: 'high', requires_person: true,
        title: 'VTI 买入提案待复核', detail: '研究已完成，等待你查看提案并作出决定。',
        link: { page: 'approvals', params: { proposal: 'proposal-preview' } } },
      { ...item, title: '持仓复查已完成', detail: '复查结果已准备好，点击查看完整报告。' },
    ];
    attention.top = attention.items[0];
    payload['/health'].warnings = ['Optional market context is unavailable.'];
    await page.evaluate(() => CT_API.readOnce('health'));
    await page.evaluate(() => CT_API.readOnce('attention'));
    assert.equal(await page.locator('[data-attention-id="order"]').isVisible(), true);
    assert.equal(await page.locator('[data-attention-id="proposal"]').isVisible(), false);
    // A notice is counted on its own line to System, never dealt among the to-dos.
    assert.equal(await page.locator('#needs-you-slot [data-od-id^="warning"]').count(), 0);
    assert.equal(await page.locator('[data-od-id="condition-notices"]').isVisible(), true);
    assert.match(await page.locator('[data-od-id="condition-notices"]').getAttribute('href'), /system\.html/);
    await screenshot('attention-stack-phone.png');
    await page.locator('#attention-rest summary').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('[data-attention-id="proposal"]').isVisible(), true);
    assert.doesNotMatch(await page.locator('#needs-you-slot').innerText(), /Optional market context/);
    attention.items[1].detail += ' Test: ' + 'LongUnbrokenSourceDetail'.repeat(12);
    await page.evaluate(() => CT_API.readOnce('attention'));
    assert.equal(await page.locator('#attention-rest').evaluate(el => el.open), true);
    for (const width of [320, 390, 1280]) {
      await page.setViewportSize({ width, height: 1000 });
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    }
    await page.setViewportSize({ width: 390, height: 1000 });
    attention.items[1].detail = '研究已完成，等待你查看提案并作出决定。';
    await page.evaluate(() => CT_API.readOnce('attention'));
    await screenshot('attention-expanded-phone.png');
    await page.locator('#attention-rest summary').click();
    await page.setViewportSize({ width: 1280, height: 900 });
    await screenshot('attention-stack-desktop.png');
    await page.evaluate(() => I18N.set('en'));
    assert.match(await page.locator('#attention-rest summary').innerText(), /2 more items/);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    assert.equal(await page.locator('[data-attention-id="order"]').evaluate(el => getComputedStyle(el).animationName), 'none');
    await page.goto(origin + '/ui/approvals.html?proposal=' + expired.proposal_id);
    await page.waitForFunction(() => document.getElementById('d-id').textContent === 'expired-attention');
    assert.equal(await page.locator('#btn-approve').isDisabled(), true);
    assert.equal(posts.length, 1, 'navigation and subsequent polls must not repeat the receipt');
    assert.deepEqual(errors, []);
    console.log('Attention browser check passed: result receipt, empty/partial states, stack disclosure, polling, responsive layout, languages, reduced motion, expired proposal.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ page: location.pathname, text: document.body.innerText.slice(-3000),
      resources: window.CT_API?.state.resources })));
    console.error(errors); throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
