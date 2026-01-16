// Offline tests of request state and rendering, without a browser or a trading venue.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '..');

function context(demo = false) {
  const elements = new Map(), listeners = new Map(), intervals = [];
  const element = id => {
    if (!elements.has(id)) elements.set(id, { innerHTML: '', textContent: '', value: '', checked: false,
      disabled: false, hidden: false, style: {}, dataset: {}, events: {}, className: '',
      classes: new Set(),
      get classList() { const s = this.classes; return {
        add: (...n) => n.forEach(x => s.add(x)), remove: (...n) => n.forEach(x => s.delete(x)),
        toggle: (n, on) => (on === undefined ? (s.has(n) ? s.delete(n) : s.add(n)) : on ? s.add(n) : s.delete(n)),
        contains: n => s.has(n) }; },
      addEventListener(event, fn) { this.events[event] = fn; },
      showModal() { this.open = true; }, close() { this.open = false; },
      querySelectorAll() { return []; }, setAttribute() {},
    });
    return elements.get(id);
  };
  const document = { hidden: false, getElementById: element, querySelector: element,
    addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); },
    dispatchEvent(event) { for (const fn of listeners.get(event.type) || []) fn(event); },
  };
  const ctx = vm.createContext({ document, URL, URLSearchParams, AbortController, console, crypto: require('node:crypto').webcrypto,
    location: { search: demo ? '?demo=1' : '', href: 'https://console.test/ui/approvals.html', origin: 'https://console.test' },
    Date, setTimeout, clearTimeout, setInterval(fn) { intervals.push(fn); return intervals.length; }, clearInterval() {},
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    fetch: async () => { throw Error('unexpected network request'); },
  });
  ctx.window = ctx;
  // The key stands in for the copy, with the interpolated values appended, so a test can
  // assert on the numbers a screen puts into a sentence without depending on its wording.
  ctx.I18N = { t: (key, vars) => vars ? key + ' ' + Object.values(vars).join(' ') : key,
    lang: 'en', LANGS: { en: 'EN' } };
  ctx.run = file => vm.runInContext(fs.readFileSync(path.join(root, 'ui/assets', file), 'utf8'), ctx, { filename: file });
  ctx.run('data.js');
  ctx.elements = elements; ctx.element = element; ctx.intervals = intervals;
  return ctx;
}
const response = (data, status = 200) => ({ ok: status === 200, status, statusText: 'failed', json: async () => data });
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(setImmediate); };

function advisorPage(post) {
  const c = context(); c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT.advisor = { notes: { text: 'Saved plan', revision: 1 }, jobs: [], decisions: [],
    snapshot: { accounts: [], candidates: [{ symbol: 'VTI', sources: ['watchlists:<script>'] }] } };
  c.CT_API = { writesEnabled: true, post, readOnce: async () => {},
    fetchJSON: async () => ({ messages: [] }) };
  c.run('pages/advisor.js');
  return c;
}

test('assistant safely renders sources and preserves new edits during a note save', async () => {
  let finish;
  const c = advisorPage(() => new Promise(resolve => { finish = resolve; }));
  assert.ok(c.element('candidate-list').innerHTML.includes('&lt;script&gt;'));
  const input = c.element('personal-notes');
  input.value = 'First edit'; input.events.input();
  const save = c.element('save-notes').events.click();
  input.value = 'Second edit'; input.events.input();
  finish({ revision: 2, text: 'First edit' }); await save;
  c.CT.advisor.notes = { revision: 2, text: 'First edit' }; c.render();
  assert.equal(input.value, 'Second edit');
});

test('assistant retries an uncertain request with the same id and keeps failed text', async () => {
  const ids = []; let calls = 0;
  const c = advisorPage(async (url, body) => {
    ids.push(body.request_id);
    if (++calls === 1) throw Error('network lost');
    return { run_key: 'advisor_test' };
  });
  c.element('chat-input').value = 'What fits my goals?';
  c.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(c.element('chat-input').value, 'What fits my goals?');
  c.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(ids.length, 2); assert.equal(ids[0], ids[1]);
  assert.equal(c.element('chat-input').value, '');
});

test('live starts empty; demo never reads or writes the API', async () => {
  const live = context();
  assert.equal(live.CT.proposalDetail, null);
  assert.equal(live.CT.proposals.length, 0);
  assert.equal(Object.keys(live.CT.portfolio.buckets).length, 0);
  const demo = context(true); let calls = 0;
  demo.fetch = async () => { calls++; return response({}); };
  demo.run('api.js'); demo.CT_API.start(['health', 'portfolio']);
  await demo.CT_API.readOnce('proposals');
  assert.equal((await demo.CT_API.post('/proposals/x/approve', {})).blocked, true);
  await flush(); assert.equal(calls, 0);
  assert.ok(demo.CT.proposals.length);
});

test('one successful endpoint cannot hide another failure or enable approval', async () => {
  const c = context();
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : url === '/health' ? response({ status: 'READY' }) : response({ detail: 'portfolio offline' }, 500);
  c.run('api.js'); c.CT_API.start(['health', 'portfolio']); await flush();
  assert.equal(c.CT_API.isLive('health'), true);
  assert.equal(c.CT_API.isLive('portfolio'), false);
  assert.equal(c.CT_API.state.online, false);
  assert.equal(c.CT_API.canAct(['health', 'portfolio']), false);
  assert.equal(Object.keys(c.CT.portfolio.buckets).length, 0);
  c.CT_API.state.resources.health.lastOkAt = Date.now() - 61000;
  assert.equal(c.CT_API.isLive('health'), false);
});

test('venue failure preserves local orders with an explicit incomplete state', async () => {
  const c = context();
  c.fetch = async () => response({ fills: [], open_orders: [], tracked_orders: [{ status: 'UNKNOWN' }], error: 'venue offline' });
  c.run('api.js'); await c.CT_API.readOnce('orders');
  assert.equal(c.CT.orders.tracked_orders[0].status, 'UNKNOWN');
  assert.equal(c.CT_API.loaded(['orders']), true);
  assert.equal(c.CT_API.isLive('orders'), false);
  assert.equal(c.CT_API.state.resources.orders.error, 'venue offline');
});

test('late proposal detail never attaches to a different queue item', async () => {
  const c = context(); let resolveA;
  c.CT.proposals = [{ proposal_id: 'A', version: 'a' }];
  c.fetch = async url => url.endsWith('/A') ? new Promise(resolve => { resolveA = resolve; })
    : response({ proposal_id: 'B', version: 'b' });
  c.run('api.js'); const pending = c.CT_API.readOnce('proposalDetail');
  c.CT.proposals = [{ proposal_id: 'B', version: 'b' }];
  resolveA(response({ proposal_id: 'A', version: 'a' })); await pending; await flush();
  assert.equal(c.CT.proposalDetail.proposal_id, 'B');
});

test('same-id, different-version detail is rejected', async () => {
  const c = context(); c.CT.proposals = [{ proposal_id: 'A', version: 'new' }];
  c.fetch = async () => response({ proposal_id: 'A', version: 'old' });
  c.run('api.js'); await c.CT_API.readOnce('proposalDetail');
  assert.equal(c.CT.proposalDetail, null);
  assert.equal(c.CT_API.isLive('proposalDetail'), false);
});

test('all generic text and attribute renderers escape untrusted HTML', () => {
  const c = context(); c.run('console.js');
  const attack = '\"><img src=x onerror=alert(1)><script>alert(1)</script>\'';
  for (const html of [c.Console.listRow({ title: attack, sub: attack, value: attack, note: attack, id: attack, trace: attack, href: 'javascript:alert(1)' }),
    c.Console.gateRow({ id: attack, name: attack, detail: attack }), c.Console.md(attack),
    c.Console.ladderMarkup(attack, 'core')]) {
    assert.ok(!html.includes('<img'));
    assert.ok(!html.includes('<script'));
    assert.ok(!html.includes('href="javascript:'));
  }
  assert.equal(c.Console.escapeHtml(attack).includes('&quot;'), true);
});

function approvalPage() {
  const c = context(true); c.run('console.js'); c.run('approval-intent.js');
  c.CT.proposalDetail = { ...c.CT.proposalDetail, proposal_id: 'A', version: 'a'.repeat(64),
    ticker: 'VTI', status: 'WAITING_FOR_APPROVAL', is_real_money: false, sell_quantity: null };
  c.CT.health.paused = false; c.CT.health.market.is_open = true;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { canAct: () => true, readOnce: async () => {}, post: async () => ({ status: 'SUBMITTED', message: 'pending' }) };
  c.run('pages/approvals.js');
  return c;
}

test('open approval dialog stays bound to its original proposal and disables on a queue change', async () => {
  const c = approvalPage(); let posted = 0; c.CT_API.post = async () => { posted++; };
  c.element('btn-approve').events.click();
  const title = c.element('approve-dialog-title').textContent;
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  assert.equal(c.element('approve-submit').disabled, false);
  c.CT.proposalDetail = { ...c.CT.proposalDetail, proposal_id: 'B', ticker: 'VOO', version: 'b'.repeat(64) };
  c.render();
  assert.equal(c.element('approve-dialog-title').textContent, title);
  assert.equal(c.element('approve-confirm').checked, false);
  assert.equal(c.element('approve-submit').disabled, true);
  await c.element('approve-submit').events.click(); assert.equal(posted, 0);
});

test('double-click sends one explicit versioned approval and pending is not a completed fill', async () => {
  const c = approvalPage(); let posted = 0, sent, resolve;
  c.CT_API.post = async (url, body) => { posted++; sent = { url, body }; return new Promise(r => { resolve = r; }); };
  c.element('btn-approve').events.click();
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  const first = c.element('approve-submit').events.click();
  await c.element('approve-submit').events.click();
  assert.equal(posted, 1); assert.equal(sent.url, '/proposals/A/approve');
  assert.equal(sent.body.confirm, true); assert.equal(sent.body.version, 'a'.repeat(64));
  assert.match(sent.body.idempotency_key, /^[0-9a-f-]{36}$/);
  resolve({ status: 'SUBMITTED', message: 'pending' }); await first;
  assert.ok(c.element('result-slot').innerHTML.includes('Submitted'));
  assert.ok(!c.element('result-slot').innerHTML.includes('card-real'));
});

function homePage(portfolio) {
  const c = context(true); c.run('console.js');
  if (portfolio) c.CT.portfolio = portfolio;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { canAct: () => true, readOnce: async () => {}, isLive: () => true };
  c.run('pages/index.js');
  return c;
}

test('the headline equity is one account, never the real and paper books added together', () => {
  const c = homePage();
  const A = c.CT.portfolio.accounts;
  assert.equal(c.element('equity-value').textContent, '$3,000.00');
  assert.notEqual(A.conceptual.equity_usd, c.CT.portfolio.combined.equity_usd);
  // the mark it is measured against belongs to the same account
  assert.ok(c.element('hwm-foot').textContent.includes('$3,000.00'));
  // the other account keeps its own row rather than being folded into the total
  assert.ok(c.element('account-list').innerHTML.includes('$100.00'));
  assert.ok(c.element('equity-source').textContent.length > 0);
});

test('money paid in is shown as funding, not as a result', () => {
  const c = homePage();
  const funded = c.element('funding-kv').innerHTML;
  assert.ok(funded.includes('$3,000.00'));       // net deposits
  assert.ok(funded.includes('+$0.00'));          // investment result, separate from funding
  assert.ok(funded.includes('0.00%'));           // return measured against money paid in
});

test('an unreadable account degrades to an explicit gap, not a zero', () => {
  const c = homePage({ as_of: '2026-09-02', buckets: {}, accounts: {},
    errors: { conceptual: 'portfolio state file not found' },
    combined: { equity_usd: 0, accounts: [], complete: false, mixes_real_and_conceptual: false } });
  assert.equal(c.element('equity-value').textContent, '—');
  assert.ok(c.element('equity-source').textContent.includes('not found'));
});

function systemPage(overrides) {
  const c = context(true); c.run('console.js');
  Object.assign(c.CT, overrides || {});
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.posted = [];
  c.CT_API = { canAct: () => true, isLive: () => true,
    readOnce: async () => {},
    post: async (url, body) => { c.posted.push({ url, body }); return { revoked: 1 }; } };
  c.run('pages/system.js');
  return c;
}

test('each signed-in device is listed with its own sign-out control', () => {
  const c = systemPage();
  const html = c.element('device-list').innerHTML;
  assert.ok(html.includes('Mac · Chrome'));
  assert.ok(html.includes('iPhone · Safari'));
  assert.ok(html.includes('sy.thisDevice'));                 // the current one is marked
  assert.ok(html.includes('data-revoke="a1b2c3d4e5f60718"'));
  assert.ok(html.includes('data-revoke="99aa88bb77cc66dd"'));
  // no token or hash is ever rendered, only the opaque session id
  assert.ok(!html.toLowerCase().includes('token'));
  assert.equal(c.element('dev-revoke-others').hidden, false);
});

test('the device list survives an endpoint that has not answered yet', () => {
  const c = systemPage({ authSessions: null });
  assert.equal(c.element('device-list').innerHTML, '');
  assert.equal(c.element('dev-revoke-others').hidden, true);
});

test('sign-out is hidden when this is the only device', () => {
  const c = systemPage({ authSessions: { max_age_days: 30, sessions: [
    { session_id: 'only', label: 'Mac · Chrome', current: true, days_left: 30,
      created_at: '2026-09-04T23:20:54+00:00', last_seen_at: '2026-09-04T23:20:54+00:00',
      expires_at: '2026-10-04T23:20:54+00:00' }] } });
  assert.equal(c.element('dev-revoke-others').hidden, true);
});

function activityPage(fetchImpl) {
  const c = context(true); c.run('console.js');
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.requested = [];
  c.CT_API = { canAct: () => true, isLive: () => true, readOnce: async () => {},
    fetchJSON: async (path) => { c.requested.push(path); return fetchImpl(path); } };
  c.CT_DEMO = false;
  c.run('pages/activity.js');
  return c;
}

const page = (ids, next) => ({
  next_cursor: next,
  events: ids.map(id => ({ id, at: '2026-09-04T01:33:01.850146+00:00', kind: 'proposal_created',
    subject: 'p' + id, actor: 'system', outcome: 'PROPOSED',
    description: 'a proposal was created', detail: { ticker: 'VTI', notional_usd: 35 } })),
});

test('the audit trail pages by row id and never repeats an entry', async () => {
  const seen = [];
  const c = activityPage(path => { seen.push(path); return page(path.includes('cursor=43') ? [42, 41] : [44, 43], path.includes('cursor=43') ? null : 43); });
  await flush();
  assert.ok(seen[0].includes('/activity?limit=25'));
  assert.equal(c.element('audit-more').hidden, false);

  await c.element('audit-more').events.click();
  await flush();
  assert.ok(seen[1].includes('cursor=43'));
  const rendered = c.element('audit-list').innerHTML;
  for (const id of [44, 43, 42, 41]) assert.ok(rendered.includes('event-' + id), 'missing ' + id);
  assert.equal(c.element('audit-more').hidden, true);       // the last page ends
});

test('every filter is offered and the first request of one starts from the newest entry', async () => {
  const seen = [];
  const c = activityPage(path => { seen.push(path); return page([44], null); });
  await flush();
  for (const group of ['all', 'proposals', 'fills', 'research', 'system']) {
    assert.ok(c.element('audit-filters').innerHTML.includes('data-group="' + group + '"'), group);
  }
  assert.equal(seen.length, 1);
  assert.ok(seen[0].includes('/activity?limit=25'));
  assert.ok(!seen[0].includes('cursor='));     // a fresh filter never resumes an old cursor
});

test('an audit trail that cannot be read says so instead of showing nothing', async () => {
  const c = activityPage(() => { throw new Error('login required'); });
  await flush();
  assert.ok(c.element('audit-note').textContent.includes('login required'));
});

/* ── the translation dictionary against the pages that use it ──────────────
   Both halves of this caught something real: twelve keys on the system page
   that no dictionary defined, and a stale second copy of the whole dictionary
   in ui/ that no page loaded, so edits to it changed nothing. A missing key
   renders as its own name — "sy.budget" where a label should be — which is
   visible but only to whoever happens to open that page. */
const uiFile = f => fs.readFileSync(path.join(root, 'ui', f), 'utf8');
const pages = fs.readdirSync(path.join(root, 'ui')).filter(f => f.endsWith('.html'));
const dictionary = () => {
  const src = uiFile('assets/i18n.js');
  const block = lang => {
    const m = src.match(new RegExp(`\\n    ${lang}: \\{([\\s\\S]*?)\\n    \\},?\\n`));
    return new Set([...m[1].matchAll(/'([\w.]+)':/g)].map(x => x[1]));
  };
  return { en: block('en'), zh: block('zh') };
};

test('every key a page asks for is defined, in both languages', () => {
  const { en, zh } = dictionary();
  const used = new Set();
  for (const file of pages)
    for (const m of uiFile(file).matchAll(/data-i18n(?:-[a-z]+)?="([^"]+)"/g))
      m[1].split(/\s+/).forEach(k => used.add(k));

  assert.ok(used.size > 100, 'the pages should be reading a lot of copy');
  assert.deepEqual([...used].filter(k => !en.has(k)), [], 'used in a page, missing from en');
  assert.deepEqual([...used].filter(k => !zh.has(k)), [], 'used in a page, missing from zh');
});

test('neither language carries copy the other is missing', () => {
  const { en, zh } = dictionary();
  assert.deepEqual([...en].filter(k => !zh.has(k)), [], 'in en, missing from zh');
  assert.deepEqual([...zh].filter(k => !en.has(k)), [], 'in zh, missing from en');
});

test('every script and stylesheet a page loads is actually shipped', () => {
  for (const file of pages)
    for (const m of uiFile(file).matchAll(/(?:src|href)="((?!https?:|\/\/|#|mailto:)[^"]+)"/g)) {
      const target = m[1].split(/[?#]/)[0];
      if (target.endsWith('.html')) continue;             // navigation, not an asset
      assert.ok(fs.existsSync(path.join(root, 'ui', target)),
                `${file} loads ${target}, which does not exist`);
    }
});
