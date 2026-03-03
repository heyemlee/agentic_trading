// Offline research check: static UI only; all API traffic uses synthetic responses.
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
  const record = (run_id, scope, account_id, more = {}) => ({ run_id, scope, account_id,
    account_scope_kind: 'single', account_label: account_id, ticker: run_id.toUpperCase(),
    bucket: scope === 'agentic' ? 'micro_live' : scope === 'demo' ? 'core' : 'advisory',
    task_type: scope === 'personal' ? 'analysis' : 'full_research', final_action: 'HOLD',
    route: { name: 'tradingagents', inferred: true, package_version: 'test', models: { quick: 'test-model' } },
    facts_as_of: null, sources_retrieved_at_max: now, counter_evidence: 'Synthetic counter-evidence <untrusted>',
    counter_evidence_truncated: true, confidence: 70, applicable_floor: scope === 'personal' ? null : 65,
    trade_date: '2026-09-18', ...more });
  const records = [record('live', 'agentic', 'micro_live'), record('paper', 'demo', 'conceptual'),
    record('new_a', 'personal', 'Account A'), record('old_a', 'personal', 'Account A', { superseded_by: 'new_a' }),
    record('only_b', 'personal', 'Account B'),
    record('whole', 'personal', null, { account_scope_kind: 'all_personal', account_ids: ['Account A', 'Account B'], account_label: 'All personal', task_type: 'portfolio_review' }),
    record('legacy', 'unspecified', null, { account_scope_kind: 'unknown' })];
  const jobs = records.map(row => ({ ...row, run_key: row.run_id, state: 'COMPLETE', opened_at: null, created_at: now }));
  const payload = {
    '/auth/session': { authenticated: true, username: 'preview', csrf_token: 'test-only' },
    '/health': { ...data.health, checked_at: now }, '/portfolio': { ...data.portfolio, checked_at: now },
    '/advisor': data.advisor, '/proposals': [], '/attention': { checked_at: now, readable: true, items: [] },
    '/accounts': { checked_at: now, accounts: ['Account A', 'Account B'].map(account_id => ({ scope: 'personal', account_scope_kind: 'single', account_id, account_label: account_id })), personal_snapshot: { stale: false } },
  };
  const errors = [], posts = []; let delayDemo = false, heldDemo;
  const rowsFor = (url, rows, queue) => rows.filter(row => row.scope === url.searchParams.get('scope') &&
    (!url.searchParams.get('account_id') || row.account_id === url.searchParams.get('account_id') ||
      (queue || url.searchParams.get('include_all_personal') === 'true') && row.account_scope_kind === 'all_personal' && row.account_ids.includes(url.searchParams.get('account_id'))) &&
    (!url.searchParams.get('task_type') || row.task_type === url.searchParams.get('task_type')));
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
      if (url.pathname === '/research' && url.searchParams.get('scope') === 'demo' && delayDemo) { heldDemo = route; return; }
      let body = payload[url.pathname] || {};
      if (request.method() === 'POST') {
        assert.equal(request.headers()['x-csrf-token'], 'test-only');
        posts.push({ path: url.pathname, body: request.postDataJSON() });
        if (url.pathname === '/research') body = { run_key: 'submitted' };
        else {
          assert.match(url.pathname, /^\/research\/[^/]+\/opened$/);
          const key = url.pathname.split('/')[2]; body = { run_key: key, opened_at: now };
        }
      } else if (url.pathname === '/research') body = rowsFor(url, records);
      else if (url.pathname === '/research/jobs') body = rowsFor(url, jobs, true);
      else if (url.pathname.startsWith('/research/')) {
        const key = decodeURIComponent(url.pathname.split('/')[2]);
        body = url.pathname.endsWith('/trace') ? { run_id: key, final_action: 'HOLD', steps: [] } : jobs.find(row => row.run_key === key) || {};
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    const screenshot = async name => {
      if (!process.env.CT_UI_CAPTURE_DIR) return;
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name) });
    };
    await page.goto(origin + '/ui/research.html');
    await page.waitForFunction(() => window.CT.researchHistory?.[0]?.run_id === 'live');
    await page.evaluate(() => I18N.set('zh'));
    const select = scope => page.locator('#research-scopes a[href="research.html?scope=' + scope + '"]').click();
    await select('personal');
    await page.waitForFunction(() => window.CT.researchHistory?.some(row => row.run_id === 'only_b'));
    assert.equal(await page.locator('[data-od-id="start-run"]').isVisible(), false);
    await page.locator('#research-account').selectOption('Account A');
    await page.locator('#research-apply').click();
    await page.waitForFunction(() => location.search.includes('account=Account+A') && window.CT.researchHistory?.length === 2);
    assert.doesNotMatch(await page.locator('#panel-runs-all').innerText(), /ONLY_B|WHOLE/);
    assert.doesNotMatch(await page.locator('#research-jobs').innerText(), /ONLY_B|WHOLE/);
    await page.locator('#research-all').check(); await page.locator('#research-apply').click();
    await page.waitForFunction(() => window.CT.researchHistory?.length === 3);
    assert.match(await page.locator('#research-jobs').innerText(), /WHOLE/);
    await page.locator('#research-task').selectOption('analysis'); await page.locator('#research-apply').click();
    await page.waitForFunction(() => window.CT.researchHistory?.length === 2);
    assert.doesNotMatch(await page.locator('#research-jobs').innerText(), /WHOLE/);
    await page.locator('#panel-runs-all details').first().locator('summary').click();
    assert.match(await page.locator('#panel-runs-all details').first().innerText(), /事实截至\n未知/);
    await screenshot('research-records-phone.png');
    await page.locator('#panel-runs-all [data-trace="old_a"]').click();
    await page.waitForFunction(() => document.getElementById('trace-record').textContent.includes('此记录已被'));
    assert.match(await page.locator('#trace-record').innerText(), /<untrusted>/);
    assert.equal(await page.locator('#trace-record script').count(), 0);
    await page.locator('#trace-record [data-trace="new_a"]').click();
    await page.waitForFunction(() => document.getElementById('trace-head').textContent.includes('new_a'));
    await page.locator('#trace-close').click();
    await select('demo');
    await page.waitForFunction(() => window.CT.researchHistory?.[0]?.run_id === 'paper');
    assert.equal(await page.locator('#f-bucket').inputValue(), 'core');
    await page.locator('#run-form button[type="submit"]').click();
    await page.waitForFunction(() => window.CT_API.activeRun === 'submitted');
    const sent = posts.find(item => item.path === '/research').body;
    assert.equal(sent.scope, 'demo'); assert.equal(sent.bucket, 'core'); assert.equal(sent.purpose, 'candidate');
    assert.match(sent.request_id, /^[0-9a-f-]{36}$/);
    await select('agentic'); await page.waitForFunction(() => window.CT.researchHistory?.[0]?.run_id === 'live');
    delayDemo = true;
    const requested = page.waitForRequest(request => request.url().includes('/research?') && request.url().includes('scope=demo'));
    await select('demo'); await requested; await select('personal');
    assert.equal(await page.locator('[data-od-id="start-run"]').isVisible(), false);
    assert.doesNotMatch(await page.locator('#panel-runs-all').innerText(), /LIVE|PAPER/);
    await heldDemo.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([records[1]]) });
    await page.waitForFunction(() => window.CT.researchHistory?.[0]?.run_id === 'new_a');
    assert.doesNotMatch(await page.locator('#panel-runs-all').innerText(), /PAPER/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('Research browser check passed: account isolation, explicit all-account inclusion, type filtering, provenance, superseding report, scoped submission, delayed response, mobile layout.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ page: location.pathname, text: document.body.innerText.slice(-3000),
      resources: window.CT_API?.state.resources })));
    console.error(errors); throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
