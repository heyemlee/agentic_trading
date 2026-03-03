// Offline Assistant integration: a static UI and synthetic, scope-bound API responses.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const ui = path.resolve(__dirname, '../ui');
(async () => {
  const now = new Date().toISOString(), accounts = ['a'.repeat(20), 'b'.repeat(20)], posts = [], errors = [];
  const bindings = new Map();
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost'), file = path.resolve(ui, '.' + url.pathname.slice(3));
    if (!url.pathname.startsWith('/ui/') || !file.startsWith(ui + path.sep)) return res.writeHead(404).end();
    try { res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream' }).end(await fs.readFile(file)); }
    catch { res.writeHead(404).end(); }
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
      const req = route.request(), url = new URL(req.url());
      assert.equal(url.origin, origin, 'No external requests permitted');
      if (url.pathname.startsWith('/ui/')) return route.continue();
      const scope = url.searchParams.get('scope') || 'personal', account = url.searchParams.get('account_id');
      let body = {};
      if (req.method() === 'POST') {
        assert.equal(req.headers()['x-csrf-token'], 'test-only');
        const sent = req.postDataJSON(); posts.push({ path: url.pathname, body: sent });
        if (url.pathname === '/advisor/jobs') {
          if (sent.kind === 'chat') bindings.set(sent.conversation_id, { scope: sent.scope, account_scope_kind: sent.account_id || sent.scope === 'agentic' ? 'single' : 'all_personal', account_id: sent.account_id });
          body = { run_key: 'submitted' };
        } else { assert.equal(url.pathname, '/research/review-a/opened'); body = { run_key: 'review-a', opened_at: now }; }
      } else if (url.pathname === '/auth/session') body = { authenticated: true, username: 'preview', csrf_token: 'test-only' };
      else if (url.pathname === '/health') body = { status: 'READY', checked_at: now, warnings: [] };
      else if (url.pathname === '/proposals') body = [];
      else if (url.pathname === '/accounts') body = { checked_at: now, accounts: accounts.map((account_id, i) => ({ scope: 'personal', account_scope_kind: 'single', account_id, account_label: 'Account ' + (i ? 'B' : 'A') })), personal_snapshot: { stale: false } };
      else if (url.pathname === '/advisor') body = { scope, account_scope_kind: account || scope === 'agentic' ? 'single' : 'all_personal', account_id: scope === 'agentic' ? 'micro_live' : account,
        account_label: scope === 'agentic' ? 'Agentic' : account === accounts[0] ? 'Account A' : account === accounts[1] ? 'Account B' : 'All personal',
        snapshot: { fetched_at: now, accounts: [], candidates: [] }, notes: { text: 'Shared personal plan', revision: 1 }, jobs: [], decisions: [], conversation_summaries: [],
        latest_review: account === accounts[0] ? { run_key: 'review-a', account_label: 'Account A', snapshot_at: '2026-09-17T12:00:00Z', analysed_at: now, stale: true,
          priorities: [{ symbol: 'VTI', recommendation: 'Review concentration', rationale: 'Synthetic rationale <untrusted>', citations: [], missing: ['No current quote'] }], limitations: ['Saved snapshot only'] } : null };
      else if (url.pathname.startsWith('/advisor/conversations/')) { const id = url.pathname.split('/').at(-1); body = { conversation_id: id, messages: [], ...bindings.get(id) }; }
      else if (url.pathname === '/research/review-a') body = { run_key: 'review-a', state: 'COMPLETE', opened_at: null };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto(origin + '/ui/advisor.html');
    await page.waitForFunction(() => CT_API.isLive('assistant') && CT_API.isLive('accounts'));
    await page.evaluate(() => I18N.set('zh'));
    const select = async (choice, account) => {
      await page.locator('#assistant-account').selectOption(choice); await page.locator('#assistant-switch').click();
      await page.waitForFunction(id => CT.assistant?.account_id === id, account);
    };
    await page.locator('#chat-input').fill('All accounts draft');
    await select('personal:' + accounts[0], accounts[0]);
    assert.equal(await page.locator('#chat-input').inputValue(), '');
    await page.locator('#chat-input').fill('Account A draft');
    await select('personal:' + accounts[1], accounts[1]);
    assert.equal(await page.locator('#chat-input').inputValue(), '');
    await select('personal:' + accounts[0], accounts[0]);
    assert.equal(await page.locator('#chat-input').inputValue(), 'Account A draft');
    assert.equal(posts.length, 0);
    await page.locator('#review-panel summary').click();
    assert.match(await page.locator('#review-stale').innerText(), /旧快照/);
    const receipt = page.waitForResponse(res => res.url().endsWith('/research/review-a/opened'));
    await page.locator('#review-open').click();
    await page.waitForFunction(() => document.getElementById('decision-sheet').open);
    await receipt;
    assert.match(await page.locator('#decision-detail').innerText(), /<untrusted>/);
    assert.equal(await page.locator('#decision-detail script').count(), 0);
    await page.locator('[data-close-sheet="decision-sheet"]').click();
    await page.locator('#review-start').click();
    await page.waitForFunction(() => document.getElementById('advisor-status').textContent.includes('加入'));
    const review = posts.find(p => p.body.kind === 'portfolio_review');
    assert.equal(review.body.scope, 'personal'); assert.equal(review.body.account_id, accounts[0]);
    await select('agentic', 'micro_live');
    assert.equal(await page.locator('#review-panel').isVisible(), false);
    assert.equal(await page.locator('#view-discover').isVisible(), false);
    await page.locator('#chat-input').fill('Explain this account'); await page.locator('#chat-send').click();
    await page.waitForFunction(() => document.getElementById('chat-input').value === '');
    const chat = posts.find(p => p.body.kind === 'chat'); assert.equal(chat.body.scope, 'agentic'); assert.equal(chat.body.account_id, null);
    await select('personal:', null); assert.equal(await page.locator('#chat-input').inputValue(), 'All accounts draft');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.locator('#chat-input').scrollIntoViewIfNeeded();
    assert.equal(await page.locator('#chat-send').isVisible(), true);
    if (process.env.CT_UI_CAPTURE_DIR) { await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true }); await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, 'assistant-phone.png') }); }
    assert.deepEqual(errors, []);
    console.log('Assistant browser check passed: isolated drafts, scoped chat, explicit portfolio review, visible-only receipt, mobile layout.');
  } catch (error) { console.error(await page?.evaluate(() => document.body.innerText.slice(-4000))); throw error; }
  finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
