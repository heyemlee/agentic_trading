// Offline Assistant integration: a static UI and synthetic, scope-bound API responses.
const { chromium, webkit } = require('playwright');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const ui = path.resolve(__dirname, '../ui');
(async () => {
  const now = new Date().toISOString(), accounts = ['a'.repeat(20), 'b'.repeat(20)], posts = [], errors = [];
  const bindings = new Map(), messages = new Map();
  let chatJobs = [];
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost'), file = path.resolve(ui, '.' + url.pathname.slice(3));
    if (!url.pathname.startsWith('/ui/') || !file.startsWith(ui + path.sep)) return res.writeHead(404).end();
    try { res.writeHead(200, { 'Content-Type': ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' })[path.extname(file)] || 'application/octet-stream' }).end(await fs.readFile(file)); }
    catch { res.writeHead(404).end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser, page;
  try {
    const safari = process.env.CT_BROWSER_ENGINE === 'webkit';
    browser = safari ? await webkit.launch({ headless: true })
      : await chromium.launch({ channel: process.env.CT_BROWSER_CHANNEL || 'chrome', headless: true });
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
          body = { run_key: sent.kind === 'chat' ? 'chat-preview' : 'submitted' };
        } else { assert.equal(url.pathname, '/research/review-a/opened'); body = { run_key: 'review-a', opened_at: now }; }
      } else if (url.pathname === '/auth/session') body = { authenticated: true, username: 'preview', csrf_token: 'test-only' };
      else if (url.pathname === '/health') body = { status: 'READY', checked_at: now, warnings: [] };
      else if (url.pathname === '/proposals') body = [];
      else if (url.pathname === '/accounts') body = { checked_at: now, accounts: accounts.map((account_id, i) => ({ scope: 'personal', account_scope_kind: 'single', account_id, account_label: 'Account ' + (i ? 'B' : 'A') })), personal_snapshot: { stale: false } };
      else if (url.pathname === '/advisor') body = { scope, account_scope_kind: account || scope === 'agentic' ? 'single' : 'all_personal', account_id: scope === 'agentic' ? 'micro_live' : account,
        account_label: scope === 'agentic' ? 'Agentic' : account === accounts[0] ? 'Account A' : account === accounts[1] ? 'Account B' : 'All personal',
        snapshot: { fetched_at: now, accounts: [], candidates: [] }, notes: { text: 'Shared personal plan', revision: 1 }, jobs: chatJobs, decisions: [], conversation_summaries: [],
        latest_review: account === accounts[0] ? { run_key: 'review-a', account_label: 'Account A', snapshot_at: '2026-09-17T12:00:00Z', analysed_at: now, stale: true,
          priorities: [{ symbol: 'VTI', recommendation: 'Review concentration', rationale: 'Synthetic rationale <untrusted>', citations: [], missing: ['No current quote'] }], limitations: ['Saved snapshot only'] } : null };
      else if (url.pathname.startsWith('/advisor/conversations/')) { const id = url.pathname.split('/').at(-1); body = { conversation_id: id, messages: messages.get(id) || [], ...bindings.get(id) }; }
      else if (url.pathname === '/research/review-a') body = { run_key: 'review-a', state: 'COMPLETE', opened_at: null };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto(origin + '/ui/advisor.html');
    await page.waitForFunction(() => CT_API.isLive('assistant') && CT_API.isLive('accounts'));
    await page.evaluate(() => I18N.set('zh'));
    await page.waitForFunction(() => document.getElementById('chat-send').disabled);
    const assertComposer = async () => {
      const layout = await page.evaluate(() => {
        const input = document.getElementById('chat-input').getBoundingClientRect();
        const send = document.getElementById('chat-send').getBoundingClientRect();
        const shell = document.body.getBoundingClientRect();
        const dock = document.querySelector('.composer-dock').getBoundingClientRect();
        return { input: input.toJSON(), send: send.toJSON(), height: innerHeight,
          width: innerWidth, scrollWidth: document.documentElement.scrollWidth, shell: shell.toJSON(), dock: dock.toJSON(),
          bodyPosition: getComputedStyle(document.body).position };
      });
      assert.equal(layout.bodyPosition, 'relative', 'Closed-keyboard chat must not use a fixed body');
      assert.ok(layout.input.top >= 0 && layout.send.bottom <= layout.height, 'Composer must stay on screen without scrolling: ' + JSON.stringify(layout));
      assert.ok(layout.send.width >= 44 && layout.send.height >= 44, 'Send needs a phone-sized touch target');
      assert.ok(layout.scrollWidth <= layout.width, 'No horizontal page overflow');
      assert.ok(Math.abs(layout.dock.bottom - layout.height) < 2, 'Composer reaches the viewport bottom: ' + JSON.stringify(layout));
      assert.ok(Math.abs(layout.shell.height - layout.height) < 2, 'Chat fills the restored viewport');
      assert.ok(Math.abs(layout.shell.top) < 2, 'Chat is not shifted above the viewport');
      assert.equal(await page.locator('.tabbar').isVisible(), false, 'Assistant always hides bottom navigation');
      assert.equal(await page.locator('.topnav').isVisible(), false);
      assert.equal(await page.locator('#assistant-back').isVisible(), true);
    };
    const capture = async name => {
      if (!process.env.CT_UI_CAPTURE_DIR) return;
      await fs.mkdir(process.env.CT_UI_CAPTURE_DIR, { recursive: true });
      await page.screenshot({ path: path.join(process.env.CT_UI_CAPTURE_DIR, name + '.png'), animations: 'disabled' });
    };
    await assertComposer();
    await capture('assistant-welcome-phone');
    await page.locator('[data-open-sheet="conversation-sheet"]').click();
    await capture('assistant-history-phone');
    await page.locator('[data-close-sheet="conversation-sheet"]').click();
    await page.waitForFunction(() => !document.getElementById('conversation-sheet').open);
    await page.locator('#ask-risk').click();
    assert.equal(await page.locator('#chat-input').inputValue(), '我的持仓有哪些风险？');
    assert.equal(posts.length, 0, 'Suggestions fill a draft without submitting');
    await page.locator('#chat-input').press('Enter');
    assert.match(await page.locator('#chat-input').inputValue(), /\n/);
    assert.equal(posts.length, 0, 'Phone Enter inserts a newline');
    const openContext = async () => { if (!await page.locator('#context-sheet').evaluate(el => el.open)) await page.locator('#assistant-context').click(); };
    const select = async (choice, account) => {
      await openContext();
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
    await openContext();
    await page.locator('#review-panel summary').click();
    await capture('assistant-account-phone');
    assert.match(await page.locator('#review-stale').innerText(), /旧快照/);
    const receipt = page.waitForResponse(res => res.url().endsWith('/research/review-a/opened'));
    await page.locator('#review-open').click();
    await page.waitForFunction(() => document.getElementById('decision-sheet').open);
    await receipt;
    assert.match(await page.locator('#decision-detail').innerText(), /<untrusted>/);
    assert.equal(await page.locator('#decision-detail script').count(), 0);
    await page.locator('[data-close-sheet="decision-sheet"]').click();
    await openContext();
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
    await page.locator('#new-conversation').click();
    await page.locator('#chat-input').fill('我的持仓有哪些风险？');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => document.getElementById('chat-input').value === '');
    const conversation = posts.filter(p => p.body.kind === 'chat').at(-1).body.conversation_id;
    messages.set(conversation, [{ role: 'user', content: '我的持仓有哪些风险？', state: 'COMPLETE' }]);
    chatJobs = [{ run_key: 'chat-preview', state: 'RUNNING', task: { kind: 'chat', conversation_id: conversation } }];
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForSelector('#chat-progress:not([hidden])');
    await page.locator('#chat-input').fill('Pending draft');
    assert.equal(await page.locator('#chat-send').isDisabled(), true);
    assert.equal(await page.locator('#chat-welcome').isVisible(), false);
    await capture('assistant-thinking-phone');
    messages.get(conversation).push({ role: 'assistant', content: '可以先从三个方向检查。\n\n**持仓集中度**\n\n看看是否有多个标的集中在同一行业，它们可能在市场波动时一起下跌。\n\n**资金使用计划**\n\n结合你预计用钱的时间，判断目前的波动是否在可承受范围内。\n\n**研究的时效性**\n\n以上仅基于已保存的账户快照。进一步判断前，需要查看最新研究与价格。', state: 'COMPLETE' });
    chatJobs[0].state = 'COMPLETE';
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForSelector('.message-copy');
    await page.locator('#chat-input').fill('');
    await page.locator('#chat-input').blur();
    assert.equal(await page.locator('#chat-progress').isVisible(), false);
    assert.equal(await page.locator('#advisor-status').innerText(), '');
    await assertComposer();
    await capture('assistant-conversation-phone');
    // Long responses scroll independently; polling must not pull a reader away from older text.
    messages.get(conversation)[1].content += '\n\n' + ('这是离线测试中的长回答，用于检查滚动和输入框位置。\n\n').repeat(35);
    chatJobs[0].run_key = 'long-response';
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForFunction(() => document.querySelector('.chat-message.assistant').textContent.includes('离线测试'));
    await page.locator('#chat-scroll').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
    await page.waitForSelector('#chat-latest:not([hidden])');
    await page.evaluate(() => CT_API.readOnce('assistant'));
    assert.equal(await page.locator('#chat-scroll').evaluate(el => el.scrollTop), 0);
    await page.locator('#chat-latest').click();
    assert.equal(await page.locator('#chat-latest').isVisible(), false);
    if (!safari) {
      await page.context().grantPermissions(['clipboard-read', 'clipboard-write']);
      await page.locator('.message-copy').click();
      assert.match(await page.evaluate(() => navigator.clipboard.readText()), /持仓集中度/);
    }
    // Sending from history resumes following, including messages loaded after the POST.
    await page.locator('#chat-input').fill('继续分析最新消息');
    await page.locator('#chat-scroll').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
    await page.waitForSelector('#chat-latest:not([hidden])');
    await page.locator('#chat-send').click();
    await page.waitForFunction(() => document.getElementById('chat-input').value === '' && document.getElementById('chat-latest').hidden);
    const assertLatest = async () => {
      assert.ok(await page.locator('#chat-scroll').evaluate(el => el.scrollHeight - el.scrollTop - el.clientHeight < 2), 'Newest message stays at the bottom');
    };
    await assertLatest();
    messages.get(conversation).push({ role: 'user', content: '继续分析最新消息', state: 'RUNNING' });
    chatJobs = [{ run_key: 'chat-preview', state: 'RUNNING', task: { kind: 'chat', conversation_id: conversation } }];
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForFunction(() => document.querySelector('.chat-message:last-child')?.textContent.includes('继续分析最新消息'));
    await assertLatest();
    await capture('assistant-sent-latest');
    messages.get(conversation).at(-1).state = 'COMPLETE';
    messages.get(conversation).push({ role: 'assistant', content: ('后续回答应保持在最新位置。\n\n').repeat(12), state: 'COMPLETE' });
    chatJobs[0].state = 'COMPLETE';
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForFunction(() => document.querySelector('.chat-message:last-child')?.textContent.includes('后续回答'));
    await assertLatest();
    await page.locator('#chat-scroll').evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll')); });
    messages.get(conversation).at(-1).content += '\n\n手动上翻时保留阅读位置。';
    chatJobs[0].run_key = 'updated-response';
    await page.evaluate(() => CT_API.readOnce('assistant'));
    await page.waitForFunction(() => document.querySelector('.chat-message:last-child')?.textContent.includes('手动上翻'));
    assert.equal(await page.locator('#chat-scroll').evaluate(el => el.scrollTop), 0);
    await page.locator('#chat-latest').click();
    for (const [width, height] of [[320, 568], [390, 460], [844, 390], [1280, 900]]) {
      await page.setViewportSize({ width, height });
      await page.waitForFunction(() => Math.abs(document.body.getBoundingClientRect().height - innerHeight) < 1);
      await assertComposer();
      await page.locator('#assistant-context').click();
      assert.equal(await page.locator('#assistant-account').isVisible(), true);
      await page.locator('[data-close-sheet="context-sheet"]').click();
      await page.waitForFunction(() => !document.getElementById('context-sheet').open);
    }
    // Emulate a mobile visual viewport shrinking while the layout viewport stays tall.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('#chat-input').focus();
    await page.evaluate(() => {
      Object.defineProperty(visualViewport, 'height', { configurable: true, value: 460 });
      visualViewport.dispatchEvent(new Event('resize'));
    });
    await page.waitForFunction(() => document.body.classList.contains('assistant-keyboard'));
    assert.equal(await page.locator('#assistant-back').isVisible(), true, 'Back remains available with the keyboard');
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).position), 'fixed',
      'Only the open-keyboard layout uses a visual-viewport-sized fixed shell');
    assert.equal(await page.locator('.tabbar').isVisible(), false);
    assert.ok(await page.locator('#chat-send').evaluate(el => el.getBoundingClientRect().bottom <= 460));
    // iOS pans to a caret with a scroll-only event, even if the height is unchanged.
    await page.evaluate(() => {
      Object.defineProperty(visualViewport, 'offsetTop', { configurable: true, value: 170 });
      visualViewport.dispatchEvent(new Event('scroll'));
    });
    await page.waitForFunction(() => document.body.style.getPropertyValue('--chat-top') === '170px');
    const keyboardLayout = await page.evaluate(() => ({
      top: document.getElementById('chat-input').getBoundingClientRect().top,
      bottom: document.querySelector('.composer-dock').getBoundingClientRect().bottom,
      heading: document.querySelector('.assistant-heading').getBoundingClientRect().top,
    }));
    assert.ok(keyboardLayout.top >= 170 && keyboardLayout.heading >= 170, 'Caret and heading stay in the panned visual viewport');
    assert.ok(Math.abs(keyboardLayout.bottom - 630) < 2, 'Composer stays at the keyboard, without a large blank gap');
    await capture('assistant-keyboard-offset');
    await page.locator('#view-discover').click();
    assert.equal(await page.locator('.tabbar').isVisible(), false, 'Discover is part of the same immersive assistant');
    assert.equal(await page.locator('#assistant-back').isVisible(), true);
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).position), 'static');
    await page.locator('#view-chat').click();
    await page.locator('#chat-input').focus();
    await page.waitForFunction(() => document.body.classList.contains('assistant-keyboard'));
    await page.locator('#assistant-context').click();
    assert.equal(await page.locator('#assistant-account').isVisible(), true);
    await page.locator('[data-close-sheet="context-sheet"]').click();
    await page.waitForFunction(() => !document.getElementById('context-sheet').open);
    // After dismissal, both offset and height can remain stale. A sub-keyboard
    // height difference must not leave the navigation suspended above the bottom.
    await page.evaluate(() => {
      Object.defineProperty(visualViewport, 'height', { configurable: true, value: innerHeight - 96 });
      document.dispatchEvent(new Event('ct:route'));
    });
    await page.waitForFunction(() => !document.body.classList.contains('assistant-keyboard') &&
      document.body.style.getPropertyValue('--chat-top') === '0px');
    await assertComposer();
    await capture('assistant-restored-viewport');
    await page.evaluate(() => {
      window.scrollTo(0, 300);
      delete visualViewport.height;
      delete visualViewport.offsetTop;
      window.dispatchEvent(new Event('pageshow'));
    });
    await page.waitForFunction(() => scrollY === 0);
    await assertComposer();
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.waitForFunction(() => Math.abs(document.body.getBoundingClientRect().height - innerHeight) < 1);
    await page.evaluate(() => I18N.set('en'));
    await page.locator('#new-conversation').click();
    await assertComposer();
    await capture('assistant-desktop');
    assert.deepEqual(errors, []);
    console.log('Assistant browser check passed: isolated accounts, review receipts, prompt drafts, pending/completed replies, copy, send/reply following, independent history scroll, stale viewport restoration, 320px/keyboard-height/landscape/desktop layouts.');
  } catch (error) { console.error(await page?.evaluate(() => document.body.innerText.slice(-4000))); throw error; }
  finally { await browser?.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; });
