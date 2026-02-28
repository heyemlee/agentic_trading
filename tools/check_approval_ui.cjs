// Offline browser check: synthetic authority changes; no API server, credentials or venue.
// node tools/check_approval_ui.cjs (Playwright + Chrome; optional CT_UI_CAPTURE_DIR)
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
  const data = fixture.window.CT;
  const now = new Date().toISOString();
  const proposal = { ...data.proposalDetail, authority_version: null, created_at: now,
    authority_changed: { from: null, to: 'authority-a', classification: 'unknown',
      paths: ['buckets.micro_live.autonomy.open.enabled'], detail: 'authority version not recorded at proposal time' } };
  const authority = { version: 'authority-a', checked_at: now, preflight: { ready: true, stale: false },
    actions: { BUY: { human_approval: { permitted: true, blocked_by: [] } } } };
  const payload = {
    '/auth/session': { authenticated: true, username: 'preview', csrf_token: 'test-only' },
    '/health': { ...data.health, checked_at: now, paused: false, market: { is_open: true }, micro_live_enabled: true },
    '/micro-live': { ...data.microLive, enabled: true, status: 'READY', blocking: [], preflight_ready: true, checked_at: now },
    '/config/authority': authority, '/proposals': [proposal],
    ['/proposals/' + proposal.proposal_id]: proposal,
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
    page.on('console', message => { if (message.type() === 'error') console.error(message.text()); });
    await page.addInitScript(() => sessionStorage.setItem('ct.startup.seen.v1', '1'));
    const origin = 'http://127.0.0.1:' + server.address().port;
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      assert.equal(url.origin, origin, 'the preview must never reach an external service');
      if (url.pathname.startsWith('/ui/')) return route.continue();
      let status = 200, body = payload[url.pathname] || {};
      if (request.method() === 'POST') {
        assert.equal(url.pathname, '/proposals/' + proposal.proposal_id + '/approve');
        const submitted = request.postDataJSON(); posts.push(submitted);
        assert.equal(submitted.acknowledge_authority_change, true);
        assert.equal(request.headers()['x-csrf-token'], 'test-only');
        if (posts.length === 1) {
          assert.equal(submitted.authority_version, 'authority-a');
          authority.version = 'authority-b'; proposal.authority_changed.to = 'authority-b';
          status = 409; body = { detail: 'authority changed since the screen loaded; reload and review' };
        } else {
          assert.equal(submitted.authority_version, 'authority-b');
          body = { status: 'SUBMITTED', message: 'Synthetic pending order' };
        }
      }
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto(origin + '/ui/approvals.html');
    await page.waitForFunction(() => !document.getElementById('btn-approve').disabled);
    await page.evaluate(() => I18N.set('zh'));
    await page.locator('#btn-approve').click();
    assert.equal(await page.locator('#approve-authority-change').isVisible(), true);
    await page.locator('#approve-confirm').check();
    assert.equal(await page.locator('#approve-submit').isDisabled(), true);
    await page.locator('#approve-authority-confirm').check();
    assert.equal(await page.locator('#approve-submit').isEnabled(), true);
    assert.equal(await page.locator('#approve-dialog').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    if (process.env.CT_UI_CAPTURE_DIR) {
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, 'approval-authority-phone.png') });
    }
    await page.locator('#approve-submit').click();
    await page.waitForFunction(() => document.getElementById('result-slot').textContent.includes('请重新核对提案'));
    assert.equal(posts.length, 1, '409 must not replay a write');
    await page.locator('#btn-approve').click();
    assert.equal(await page.locator('#approve-confirm').isChecked(), false);
    assert.equal(await page.locator('#approve-authority-confirm').isChecked(), false);
    assert.match(await page.locator('#approve-authority-summary').innerText(), /authority-b/);
    await page.locator('#approve-confirm').check(); await page.locator('#approve-authority-confirm').check();
    await page.locator('#approve-submit').click();
    await page.waitForFunction(() => document.getElementById('result-slot').textContent.includes('Synthetic pending order'));
    assert.equal(posts.length, 2);
    assert.notEqual(posts[0].idempotency_key, posts[1].idempotency_key);
    assert.deepEqual(errors, []);
    console.log('Approval browser check passed: separate acknowledgement, version conflict, explicit retry, mobile layout.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ errors: document.body.innerText.slice(-2500),
      resources: window.CT_API?.state.resources, proposal: window.CT?.proposalDetail?.proposal_id,
      authority: window.CT?.authority, matches: window.ApprovalIntent?.authorityMatches(window.CT?.proposalDetail, window.CT?.authority) })));
    console.error(errors);
    throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
