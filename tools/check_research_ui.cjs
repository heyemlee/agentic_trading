// Offline Lab research check: static UI only; all API traffic uses synthetic responses.
// Playwright + Chrome; optional CT_UI_CAPTURE_DIR for screenshots.
//
// Research lives in the Lab (2026-09-23): the office, then what the owner holds and what they
// added, each name's newest report opening over the room. This drives that page on a phone and
// holds what Research used to be held to: a Demo book's run and a record bound to no account are
// never drawn, a link bound to an account refuses another account's report, a report is marked
// read only once shown, and research starts only for the account a name belongs to.
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
  const A = 'a'.repeat(20);
  const record = (run_id, scope, account_id, more = {}) => ({ run_id, scope, account_id,
    account_scope_kind: 'single', account_label: account_id, ticker: run_id.toUpperCase(),
    bucket: scope === 'agentic' ? 'micro_live' : scope === 'demo' ? 'core' : scope === 'personal' ? 'advisory' : null,
    task_type: scope === 'personal' ? 'analysis' : 'full_research', final_action: 'HOLD', instrument_type: 'etf',
    route: { name: 'tradingagents', inferred: true, package_version: 'test', models: { quick: 'test-model' } },
    facts_as_of: null, sources_retrieved_at_max: now, counter_evidence: 'Synthetic counter-evidence <untrusted>',
    counter_evidence_truncated: true, confidence: 70, applicable_floor: scope === 'personal' ? null : 65,
    trade_date: '2026-09-18', created_at: now, ...more });
  const records = [record('paper', 'demo', 'conceptual'), record('legacy', 'unspecified', null, { account_scope_kind: 'unknown' }),
    record('live', 'agentic', 'micro_live', { ticker: 'VTI' }),
    record('aapl_new', 'personal', A, { ticker: 'AAPL', account_label: '••••1111' }),
    record('aapl_old', 'personal', A, { ticker: 'AAPL', account_label: '••••1111', superseded_by: 'aapl_new' }),
    record('other', 'personal', 'b'.repeat(20), { ticker: 'KO', account_label: '••••2222' })];
  const jobs = records.map(row => ({ ...row, run_key: row.run_id, state: 'COMPLETE', opened_at: null, cancellable: false }));
  const subject = (symbol, holders, latest, start, lists = []) => ({ symbol, holders, lists, latest, running: null, start });
  const subjects = { checked_at: now, errors: [],
    snapshot: { read: true, fetched_at: now, age_seconds: 60, errors: [] },
    held: [subject('VTI', [{ scope: 'agentic', account_id: 'micro_live', account_label: 'Agentic' }], records[2],
                   { scope: 'agentic', bucket: 'micro_live', purpose: 'review_holding' }),
           subject('AAPL', [{ scope: 'personal', account_id: A, account_label: '••••1111' }], records[3],
                   { scope: 'personal', kind: 'analysis', account_id: A }, ['Core'])],
    added: [subject('NVDA', [], null, { scope: 'agentic', bucket: 'micro_live', purpose: 'candidate' }, ['AI & chips'])] };
  const payload = {
    '/auth/session': { authenticated: true, username: 'preview', csrf_token: 'test-only' },
    '/health': { ...data.health, checked_at: now }, '/proposals': [], '/attention': { checked_at: now, readable: true, items: [] },
    '/system/schedule': data.schedule, '/research/roster': data.labRoster, '/research/subjects': subjects,
    '/research': records, '/research/jobs': jobs,
  };
  const errors = [], posts = [];
  const until = async (test, what) => {
    for (let i = 0; i < 200; i++) { if (test()) return; await new Promise(resolve => setTimeout(resolve, 25)); }
    throw new Error('timed out waiting for ' + what);
  };
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/ui/')) { res.writeHead(404).end(); return; }
    const file = path.resolve(ui, '.' + url.pathname.slice(3));
    if (!file.startsWith(ui + path.sep)) { res.writeHead(403).end(); return; }
    try {
      const body = await fs.readFile(file);
      const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };
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
        assert.equal(request.headers()['x-csrf-token'], 'test-only');
        posts.push({ path: url.pathname, body: request.postDataJSON() });
        if (url.pathname === '/research') body = { run_key: 'submitted' };
        else if (url.pathname === '/advisor/jobs') body = { run_key: 'advisor_submitted' };
        else {
          assert.match(url.pathname, /^\/research\/[^/]+\/opened$/);
          body = { run_key: url.pathname.split('/')[2], opened_at: now };
        }
      } else if (/^\/research\/[^/]+(\/trace)?$/.test(url.pathname) && !payload[url.pathname]) {
        const key = decodeURIComponent(url.pathname.split('/')[2]);
        const row = jobs.find(job => job.run_key === key) || {};
        body = url.pathname.endsWith('/trace') ? { run_id: key, ticker: row.ticker, final_action: 'HOLD', steps: [] } : row;
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    const screenshot = async name => {
      if (!process.env.CT_UI_CAPTURE_DIR) return;
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name) });
    };

    await page.goto(origin + '/ui/lab.html');
    await page.waitForSelector('#held-list .lab-subject');
    await page.evaluate(() => I18N.set('zh'));
    assert.equal(await page.locator('[data-od-id="tab-lab"]').getAttribute('aria-current'), 'page', 'the Lab is the third tab');
    // A Demo book's run and a record bound to no account are never drawn in the office.
    assert.equal(await page.locator('#symbol').innerText(), 'VTI');
    assert.doesNotMatch(await page.locator('.lab-workspace').innerText(), /PAPER|LEGACY/);
    await screenshot('lab-subjects-phone.png');

    // A held name's newest report, with its provenance, and a read receipt once it is on screen.
    await page.locator('#held-list [data-trace="aapl_new"]').click();
    await page.waitForFunction(() => document.getElementById('trace-head').textContent.includes('AAPL'));
    await page.locator('#trace-record summary').click();
    assert.match(await page.locator('#trace-record').innerText(), /事实截至\n未知/);
    assert.match(await page.locator('#trace-record').innerText(), /<untrusted>/);
    assert.equal(await page.locator('#trace-record script').count(), 0);
    await until(() => posts.some(item => item.path === '/research/aapl_new/opened'), 'a shown result to be marked read');
    await screenshot('lab-report-phone.png');

    // Studied again from its report: read-only, in the account that holds it.
    await page.locator('#trace-actions [data-start="AAPL"]').click();
    await until(() => posts.some(item => item.path === '/advisor/jobs'), 'the analysis request');
    const own = posts.find(item => item.path === '/advisor/jobs').body;
    assert.equal(own.kind, 'analysis'); assert.equal(own.account_id, A); assert.equal(own.scope, 'personal');
    assert.equal(own.bucket, undefined);

    // A watched name is researched for the Agentic account, as a candidate.
    await page.locator('#added-list [data-start="NVDA"]').click();
    await until(() => posts.some(item => item.path === '/research'), 'the research request');
    const agentic = posts.find(item => item.path === '/research').body;
    assert.equal(agentic.scope, 'agentic'); assert.equal(agentic.bucket, 'micro_live'); assert.equal(agentic.purpose, 'candidate');
    assert.match(agentic.request_id, /^[0-9a-f-]{36}$/);

    // A link bound to one account refuses another account's report, and consumes no notice.
    const before = posts.filter(item => item.path.endsWith('/opened')).length;
    await page.goto(origin + '/ui/lab.html?run=other&scope=personal&account=' + A);
    await page.waitForFunction(() => document.getElementById('trace-steps').textContent.includes('无法确认') ||
      document.getElementById('trace-steps').textContent.includes('cannot be confirmed'));
    assert.equal(posts.filter(item => item.path.endsWith('/opened')).length, before);

    // A research link written before the Lab held the reports opens the Lab, and the report.
    await page.goto(origin + '/ui/lab.html');
    await page.waitForSelector('#held-list .lab-subject');
    await page.evaluate(() => {
      const link = document.createElement('a'); link.href = 'research.html?run=live';
      document.body.append(link); link.click(); link.remove();
    });
    await page.waitForURL('**/lab.html?run=live');
    await page.waitForFunction(() => document.getElementById('trace-sheet').open);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.deepEqual(errors, []);
    console.log('Lab research check passed: owner-only runs, held and added lists, provenance, read receipt, account-bound deep link, research started per account, old research links, mobile layout.');
  } catch (error) {
    console.error(await page?.evaluate(() => ({ page: location.pathname + location.search, text: document.body.innerText.slice(-3000),
      resources: window.CT_API?.state.resources })));
    console.error(errors); throw error;
  } finally {
    await browser?.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
