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
    // Pages that style a group of controls read them this way; no test drives a real
    // collection, so an empty one keeps the render path honest without a DOM.
    querySelectorAll: () => [],
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
  ctx.run = (file, edit) => {
    const src = fs.readFileSync(path.join(root, 'ui/assets', file), 'utf8');
    return vm.runInContext(edit ? edit(src) : src, ctx, { filename: file });
  };
  ctx.run('data.js');
  ctx.elements = elements; ctx.element = element; ctx.intervals = intervals;
  return ctx;
}
const response = (data, status = 200) => ({ ok: status === 200, status, statusText: 'failed', json: async () => data });
const flush = async () => { for (let i = 0; i < 10; i++) await new Promise(setImmediate); };

function tabStorage() {
  const values = new Map();
  return { getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}
function advisorPage(post, options = {}) {
  const c = context(); c.run('console.js');
  c.sessionStorage = options.storage || tabStorage();
  c.addEventListener = c.document.addEventListener.bind(c.document);
  c.run('advisor-session.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT.advisor = { notes: { text: 'Saved plan', revision: 1 }, jobs: [], decisions: [],
    snapshot: { accounts: [], candidates: [{ symbol: 'VTI', sources: ['watchlists:<script>'] }] } };
  c.CT_API = { writesEnabled: true, authenticated: true, username: options.owner || 'owner', post, readOnce: async () => {},
    fetchJSON: options.fetchJSON || (async () => ({ messages: [] })) };
  c.run('pages/advisor.js');
  return c;
}

test('drafts and selected conversation survive a page reload without posting anything', async () => {
  const storage = tabStorage(); let posts = 0;
  const post = async () => { posts++; return {}; };
  const c = advisorPage(post, { storage });
  c.element('chat-input').value = 'First conversation'; c.element('chat-input').events.input();
  c.element('new-conversation').events.click();
  c.element('chat-input').value = 'Second conversation'; c.element('chat-input').events.input();
  c.element('personal-notes').value = 'Unsaved plan'; c.element('personal-notes').events.input();
  const selected = JSON.parse(storage.getItem('ct.advisor.session.v1')).selected;
  const restored = advisorPage(post, { storage }); await flush();
  assert.equal(restored.element('chat-input').value, 'Second conversation');
  assert.equal(restored.element('personal-notes').value, 'Unsaved plan');
  assert.equal(JSON.parse(storage.getItem('ct.advisor.session.v1')).selected, selected);
  restored.document.dispatchEvent({ type: 'click', target: { closest: selector => selector === '[data-conversation]'
    ? { dataset: { conversation: 'default' } } : null } });
  assert.equal(restored.element('chat-input').value, 'First conversation');
  assert.equal(posts, 0);
});

test('conversation titles remain stable when old jobs leave the recent list', () => {
  const storage = tabStorage(), c = advisorPage(async () => ({}), { storage });
  c.CT.advisor.jobs = [{ run_key: 'job', state: 'COMPLETE', task: { kind: 'chat', conversation_id: 'default', message: 'My long-term plan' } }];
  c.render();
  const restored = advisorPage(async () => ({}), { storage });
  assert.match(restored.element('conversations').innerHTML, /My long-term plan/);
});

test('a late send response cannot erase a newer draft', async () => {
  let finish;
  const storage = tabStorage(), c = advisorPage(() => new Promise(resolve => { finish = resolve; }), { storage });
  c.element('chat-input').value = 'Send this'; c.element('chat-input').events.input();
  c.element('chat-form').events.submit({ preventDefault() {} });
  c.element('chat-input').value = 'Next question'; c.element('chat-input').events.input();
  finish({ run_key: 'ok' }); await flush();
  assert.equal(c.element('chat-input').value, 'Next question');
  assert.equal(advisorPage(async () => ({}), { storage }).element('chat-input').value, 'Next question');
});

test('reload preserves the request id for an explicit retry but never retries automatically', async () => {
  const storage = tabStorage(), ids = [];
  const first = advisorPage(async (url, body) => { ids.push(body.request_id); throw Error('lost response'); }, { storage });
  first.element('chat-input').value = 'Question'; first.element('chat-input').events.input();
  first.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  const second = advisorPage(async (url, body) => { ids.push(body.request_id); return {}; }, { storage }); await flush();
  assert.equal(ids.length, 1);
  second.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(ids.length, 2); assert.equal(ids[0], ids[1]);
  assert.equal(second.element('chat-input').value, '');
});

test('unavailable draft storage prompts before leaving, and authentication loss clears drafts', () => {
  const blocked = { getItem() { throw Error('blocked'); }, setItem() { throw Error('blocked'); }, removeItem() {} };
  const c = advisorPage(async () => ({}), { storage: blocked });
  c.element('chat-input').value = 'Keep this'; c.element('chat-input').events.input();
  let prevented = false;
  c.document.dispatchEvent({ type: 'beforeunload', preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  c.CT_API.authenticated = false;
  c.document.dispatchEvent({ type: 'ct:auth', detail: { authenticated: false, clearDrafts: true } });
  assert.equal(c.element('chat-input').value, '');
});

test('drafts cannot be restored under a different signed-in owner', () => {
  const storage = tabStorage(), c = advisorPage(async () => ({}), { storage });
  c.element('chat-input').value = 'Private draft'; c.element('chat-input').events.input();
  const other = advisorPage(async () => ({}), { storage, owner: 'another-owner' });
  assert.equal(other.element('chat-input').value, '');
  assert.ok(!storage.getItem('ct.advisor.session.v1').includes('Private draft'));
});

test('restored personal drafts retain their original revision when the saved profile changes', async () => {
  const storage = tabStorage();
  const first = advisorPage(async () => ({}), { storage });
  first.element('personal-notes').value = 'My unsaved edits'; first.element('personal-notes').events.input();
  let sent;
  const restored = advisorPage(async (url, body) => { sent = body; throw Error('revision conflict'); }, { storage });
  restored.CT.advisor.notes = { text: 'Updated on another device', revision: 2 }; restored.render();
  assert.equal(restored.element('personal-notes').value, 'My unsaved edits');
  await restored.element('save-notes').events.click();
  assert.equal(sent.revision, 1);
  assert.equal(restored.element('personal-notes').value, 'My unsaved edits');
  assert.match(restored.element('notes-save-status').textContent, /revision conflict/);
});

test('changing the signed-in owner clears an already open tab writing state', () => {
  const storage = tabStorage(), c = advisorPage(async () => ({}), { storage });
  c.element('chat-input').value = 'Private old owner draft'; c.element('chat-input').events.input();
  c.CT_API.username = 'new-owner';
  c.document.dispatchEvent({ type: 'ct:auth', detail: { authenticated: true, username: 'new-owner' } });
  c.render();
  assert.equal(c.element('chat-input').value, '');
  assert.ok(!storage.getItem('ct.advisor.session.v1').includes('Private old owner draft'));
});

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

test('assistant panels switch instantly without losing a draft', () => {
  const c = advisorPage(async () => ({}));
  assert.equal(c.element('panel-chat').hidden, false);
  assert.equal(c.element('panel-discover').hidden, true);
  c.element('chat-input').value = 'Keep my question';
  c.element('view-discover').events.click();
  assert.equal(c.element('panel-chat').hidden, true);
  assert.equal(c.element('panel-discover').hidden, false);
  assert.equal(c.element('panel-personal').hidden, true);
  c.element('view-chat').events.click();
  assert.equal(c.element('chat-input').value, 'Keep my question');
  assert.equal(c.element('panel-chat').hidden, false);
});

test('assistant activity and conversation cards use safe readable summaries and remain stable on polls', () => {
  const c = advisorPage(async () => ({}));
  c.CT.advisor.jobs = [{ run_key: 'job-1', ticker: 'ASSISTANT', state: 'COMPLETE', created_at: '2026-09-09T12:00:00Z',
    task: { kind: 'chat', conversation_id: 'default', message: 'My plan <img src=x>' } }];
  c.render();
  assert.match(c.element('advisor-jobs').innerHTML, /ux.task.chat/);
  assert.ok(!c.element('advisor-jobs').innerHTML.includes('ASSISTANT'));
  assert.match(c.element('conversations').innerHTML, /My plan &lt;img src=x&gt;/);
  const element = c.element('conversations');
  let html = element.innerHTML, replacements = 0;
  Object.defineProperty(element, 'innerHTML', { get: () => html, set: value => { replacements++; html = value; } });
  c.render(); c.render();
  assert.equal(replacements, 0, 'unchanged polling must not replace the focused card');
});

test('research detail stays bound to the opened record while new data arrives', () => {
  const c = advisorPage(async () => ({}));
  const decision = { run_id: 'run-1', ticker: 'VTI', action: 'NO_TRADE', trade_date: '2026-09-09', bucket: 'core',
    policy: { proposal: { investment_thesis: 'Original <script>', entry_logic: 'Wait for evidence' } } };
  c.CT.advisor.decisions = [decision]; c.render();
  c.document.dispatchEvent({ type: 'click', target: { closest: selector => selector === '[data-decision]'
    ? { dataset: { decision: 'run-1' } } : null } });
  assert.equal(c.element('decision-sheet').open, true);
  const original = c.element('decision-detail').innerHTML;
  assert.match(original, /Original &lt;script&gt;/);
  c.CT.advisor.decisions = [{ ...decision, ticker: 'BND' }]; c.render();
  assert.equal(c.element('decision-detail').innerHTML, original);
  assert.equal(c.element('decision-sheet').open, true);
});

test('glass sheets restore focus without clearing an unsaved personal draft', async () => {
  const c = advisorPage(async () => ({}));
  let focused = false;
  c.document.activeElement = { focus() { focused = true; } };
  c.matchMedia = () => ({ matches: true });
  c.Console.openSheet('personal-sheet');
  c.element('personal-notes').value = 'Unsaved draft'; c.element('personal-notes').events.input();
  c.Console.closeSheet('personal-sheet');
  await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(c.element('personal-sheet').open, false);
  assert.equal(focused, true);
  c.render(); c.Console.openSheet('personal-sheet');
  assert.equal(c.element('personal-notes').value, 'Unsaved draft');
});

test('candidate screening returns to the conversation where the answer arrives', async () => {
  const c = advisorPage(async () => ({ run_key: 'test' }));
  c.element('view-discover').events.click();
  assert.equal(c.element('panel-discover').hidden, false);
  c.element('shortlist').events.click(); await flush();
  assert.equal(c.element('panel-chat').hidden, false);
  assert.equal(c.element('panel-discover').hidden, true);
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

test('initial authentication is loading, then settles without granting writes on failure', async () => {
  const c = context(); let finish;
  c.fetch = async url => url === '/auth/session' ? new Promise(resolve => { finish = resolve; }) : response({}, 401);
  c.run('api.js'); c.CT_API.start(['health']);
  assert.equal(c.CT_API.state.authPending, true);
  assert.equal(c.CT_API.writesEnabled, false);
  finish(response({}, 401)); await flush();
  assert.equal(c.CT_API.state.authPending, false);
  assert.equal(c.CT_API.authenticated, false);
  assert.equal(c.CT_API.canAct(['health']), false);
});

test('portfolio keeps broker snapshots separate from strategy money and labels stale data', () => {
  const c = context(true); c.run('console.js');
  c.Console.mount = () => {};
  let optional;
  c.Console.live = (keys, render, extra) => { optional = extra; c.render = render; render(); };
  c.CT_API = { isLive: () => false };
  c.CT.advisor = { stale: true, snapshot: { fetched_at: '2026-09-09T12:00:00Z', accounts: [
    { label: 'Account <img>', equity_usd: 5000, cash_usd: null, complete: false, positions: [
      { symbol: 'AAPL', quantity: 3, average_cost: 200, market_value_usd: 600, unrealized_pct: 0 }
    ] }
  ] } };
  c.run('pages/portfolio.js');
  assert.equal(optional[0], 'advisor');
  assert.match(c.element('broker-accounts').innerHTML, /Account &lt;img&gt;/);
  assert.match(c.element('broker-accounts').innerHTML, /5,000/);
  assert.match(c.element('broker-accounts').innerHTML, /ux.incompleteAccount/);
  assert.match(c.element('broker-stamp').textContent, /ai.stale/);
  assert.equal(c.element('pf-total').textContent, c.Console.usd(c.CT.portfolio.buckets.micro_live.equity_usd));
  c.CT.advisor = {}; c.render();
  assert.equal(c.element('broker-accounts').innerHTML, '');
  assert.equal(c.element('broker-stamp').textContent, 'ai.empty');
});

function portfolioPage(post, advisor) {
  const c = context(); c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { isLive: () => true, writesEnabled: true, readOnce: async () => {}, post };
  c.CT.advisor = advisor;
  c.run('pages/portfolio.js');
  return c;
}
const savedSnapshot = { fetched_at: '2026-09-09T12:00:00Z', accounts: [] };

/* The snapshot is refreshed by an assistant job, and the button that starts
   one is on another screen. Without this the page tells the reader to refresh
   and offers nothing to press — which is how a day-old position list came to
   be read as live. */
test('a stale broker snapshot refreshes itself once per visit, not once per poll', async () => {
  const sent = [];
  const c = portfolioPage(async (path, body) => { sent.push([path, body]); return { run_key: 'advisor_1' }; },
                          { stale: true, jobs: [], snapshot: savedSnapshot });
  await flush();
  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], '/advisor/jobs');
  assert.equal(sent[0][1].kind, 'refresh');

  c.CT.advisor = { stale: true, snapshot: savedSnapshot,
                   jobs: [{ run_key: 'advisor_1', state: 'RUNNING', task: { kind: 'refresh' } }] };
  c.render(); c.render(); await flush();
  assert.equal(sent.length, 1, 'a running refresh must not be queued again every second');
  assert.match(c.element('broker-stamp').textContent, /ux.refreshing/);

  c.CT.advisor = { stale: true, snapshot: savedSnapshot,
                   jobs: [{ run_key: 'advisor_1', state: 'FAILED', task: { kind: 'refresh' },
                            error: 'Robinhood login expired' }] };
  c.render(); await flush();
  assert.match(c.element('broker-stamp').textContent, /ux.refreshFailed Robinhood login expired/);
  assert.equal(sent.length, 1, 'a failed refresh is reported, never retried in a loop');
});

test('a current snapshot, and one already being refreshed elsewhere, are left alone', async () => {
  const fresh = portfolioPage(async () => { throw Error('nothing should be posted'); },
                              { stale: false, jobs: [], snapshot: savedSnapshot });
  await flush();
  assert.match(fresh.element('broker-stamp').textContent, /ai.fresh/);

  const busy = portfolioPage(async () => { throw Error('nothing should be posted'); },
                             { stale: true, snapshot: savedSnapshot,
                               jobs: [{ run_key: 'from_assistant', state: 'QUEUED', task: { kind: 'refresh' } }] });
  await flush();
  assert.match(busy.element('broker-stamp').textContent, /ux.refreshing/);
});

/* The ledger fails closed once nothing has confirmed it for max_age_days, and the API then
   answers 200 with its buckets emptied and the reason in errors. Throwing that payload away
   left the reader on a blank screen: no figure, no reason, and no way to fix it from the one
   screen the money is on. */
test('an unconfirmed ledger still reaches the page, and still cannot be approved against', async () => {
  const c = context();
  const held = { as_of: '2026-09-11', buckets: {}, accounts: {}, combined: { equity_usd: 0 },
                 errors: { micro_live: 'portfolio state as_of 2026-09-03 is 8 days older than 2026-09-11 (limit 7)' } };
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : url === '/health' ? response({ status: 'READY' }) : response(held);
  c.run('api.js'); c.CT_API.start(['health', 'portfolio']); await flush();

  assert.deepEqual(c.CT.portfolio, held, 'the page needs the payload to say why it is empty');
  assert.equal(c.CT_API.isLive('portfolio'), false, 'it is still incomplete');
  assert.equal(c.CT_API.canAct(['health', 'portfolio']), false, 'and nothing may be approved against it');
  assert.equal(c.CT_API.loaded(['portfolio']), true);
});

function ledgerPage(post, portfolio) {
  const c = context(); c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { isLive: () => true, writesEnabled: true, readOnce: async () => { c.reread = true; }, post };
  c.CT.advisor = { stale: false, jobs: [], snapshot: { fetched_at: '2026-09-11T12:00:00Z', accounts: [] } };
  c.CT.portfolio = portfolio;
  c.run('pages/portfolio.js');
  return c;
}
const heldBack = {
  as_of: '2026-09-11', buckets: {}, accounts: {}, combined: { equity_usd: 0 },
  errors: { micro_live: 'as_of 2026-09-03 is 8 days older than 2026-09-11 (limit 7)' },
};

test('a withheld ledger shows a gap and its reason, never a zero', () => {
  const c = ledgerPage(async () => ({}), heldBack);
  assert.equal(c.element('pf-total').textContent, '—', '$0.00 would be a claim about money that is still there');
  assert.equal(c.element('ledger-notice').hidden, false);
  assert.match(c.element('ledger-reason').textContent, /8 days older/);

  const fine = ledgerPage(async () => ({}), { ...heldBack, errors: {},
    buckets: { micro_live: { equity_usd: 100, allocation_usd: 100, cash_usd: 100, positions_value_usd: 0,
                             positions: [], position_count: 0, sleeves_pct: {}, account: 'micro_live',
                             is_real_money: true, venue: 'robinhood' } } });
  assert.equal(fine.element('ledger-notice').hidden, true);
  assert.equal(fine.element('pf-total').textContent, c.Console.usd(100));
});

test('the confirm button asks for the check and reports what it refused', async () => {
  const sent = [];
  const refusing = async (path, body) => {
    sent.push([path, body]);
    return { confirmed: false, ledgers: [{ ledger: 'micro_live', error: 'PortfolioError: cash mismatch' }] };
  };
  const c = ledgerPage(refusing, heldBack);
  c.element('confirm-ledger').events.click(); await flush();

  assert.equal(sent.length, 1);
  assert.equal(sent[0][0], '/portfolio/confirm');
  assert.equal(sent[0][1].ledger, 'all');
  assert.match(c.element('confirm-status').textContent, /pf.confirmRefused.*cash mismatch/);
  assert.equal(c.element('ledger-notice').hidden, false, 'a refusal leaves the ledger where it was');

  const ok = ledgerPage(async () => ({ confirmed: true, ledgers: [{ ledger: 'micro_live', confirmed: true }] }), heldBack);
  ok.element('confirm-ledger').events.click(); await flush();
  assert.match(ok.element('confirm-status').textContent, /pf.confirmed/);
  assert.equal(ok.reread, true, 'the figures have to be read again before they can come back');
});

test('a reader who cannot write is not offered the check', () => {
  const c = ledgerPage(async () => ({}), heldBack);
  c.CT_API.writesEnabled = false; c.render();
  assert.equal(c.element('confirm-ledger').disabled, true);
});

test('shared notice card keeps the full warning and escapes its text', () => {
  const c = context(); c.run('console.js');
  const html = c.Console.noticeCard('Trading blocked <script> details', 0);
  assert.match(html, /Trading blocked &lt;script&gt; details/);
  assert.match(html, /notice-card/);
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

/* The console hides the conceptual book behind one switch in console.js. Flipping the
   source is deliberate: it exercises the real constant, so renaming the switch fails
   here rather than quietly leaving these tests asserting the same state twice. */
function withConceptual(src) {
  const out = src.replace('const SHOW_CONCEPTUAL = false;', 'const SHOW_CONCEPTUAL = true;');
  assert.notEqual(out, src, 'the SHOW_CONCEPTUAL switch was renamed; update this test');
  return out;
}

function homePage(portfolio, showConceptual = false) {
  const c = context(true); c.run('console.js', showConceptual ? withConceptual : null);
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
  assert.equal(c.element('equity-value').textContent, '$100.00');
  assert.notEqual(A.micro_live.equity_usd, c.CT.portfolio.combined.equity_usd);
  // the mark it is measured against belongs to the same account
  assert.ok(c.element('hwm-foot').textContent.includes('$100.00'));
  assert.ok(c.element('equity-source').textContent.length > 0);
});

test('with the switch off, no conceptual money reaches the home screen', () => {
  const c = homePage();
  const screen = c.element('equity-value').textContent + c.element('funding-kv').innerHTML +
    c.element('account-list').innerHTML + c.element('bucket-list').innerHTML +
    c.element('hwm-foot').textContent;
  // the paper book's equity, and each of its buckets, in the figures the page writes
  for (const paper of ['$3,000.00', '$2,500.00', '$500.00']) {
    assert.ok(!screen.includes(paper), 'the paper book leaked onto the home screen: ' + paper);
  }
});

test('the conceptual book comes back in full when the switch is on', () => {
  const c = homePage(null, true);
  assert.equal(c.element('equity-value').textContent, '$3,000.00');
  assert.ok(c.element('hwm-foot').textContent.includes('$3,000.00'));
  // the real-money account keeps its own row rather than being folded into the total
  assert.ok(c.element('account-list').innerHTML.includes('$100.00'));
  assert.equal(c.element('bucket-list').innerHTML, '', 'bucket detail belongs to Assets');
});

test('money paid in is shown as funding, not as a result', () => {
  const c = homePage();
  const funded = c.element('funding-kv').innerHTML;
  assert.ok(funded.includes('$100.00'));         // net deposits
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
  c.run('pages/notifications.js');
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
const pages = [...fs.readdirSync(path.join(root, 'ui')).filter(f => f.endsWith('.html')), 'assets/startup.html'];
const dictionary = () => {
  const src = uiFile('assets/i18n.js');
  const block = lang => {
    const m = src.match(new RegExp(`\\n    ${lang}: \\{([\\s\\S]*?)\\n    \\},?\\n`));
    return new Set([...m[1].matchAll(/'([\w.]+)':/g)].map(x => x[1]));
  };
  return { en: block('en'), zh: block('zh') };
};

test('the polling cadence stays within its budget, because two of these reads call a venue', () => {
  /* On 2026-09-15 this table was quickened across the board — micro-live, portfolio, risk and
     research from 60s to 10-15s, proposals from 15s to 5s — to make the console feel live. The
     effect was the opposite. /micro-live opens a fresh Robinhood OAuth/MCP session on every
     call, and /orders does the same whenever micro-live execution is enabled; each handshake
     runs for four to eight seconds and delays every other read while it does. At 15s the API
     was handshaking almost continuously, and a tab change waited seconds for a SQLite read
     that costs 26 ms on its own. Nothing failed, because nothing here was watching.

     So these are pinned to FRONTEND_HANDOFF.md §15 and to what ui/README.md tells the reader.
     Quickening one is a decision about the whole console: change the budget, this test and the
     README together, having looked at what the endpoint costs the API. */
  const src = uiFile('assets/api.js');
  const found = Object.fromEntries([...src.matchAll(/^\s{4}(\w+): \{[^}]*?ms: (\d+)/gm)]
    .map(m => [m[1], Number(m[2])]));
  const budget = {
    advisor: 5000, health: 30000, schedule: 60000, operations: 60000,
    notifications: 120000, pushDevices: 120000, authSessions: 60000, evaluation: 300000,
    portfolio: 60000, risk: 60000, microLive: 60000, proposals: 15000, orders: 10000,
    research: 60000, labRoster: 900000, researchJobs: 10000, researchRun: 5000,
    proposalDetail: 15000,
  };
  assert.deepEqual(found, budget);

  // The two that reach a venue are the reason the budget exists; neither may be quickened.
  assert.ok(found.microLive >= 60000, '/micro-live opens a broker session on every call');
  assert.ok(found.orders >= 10000, '/orders opens a broker session while real money is enabled');
});

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

/* Copy looked up by a status the API returns reads `C.psCopy(status)` or `C.sysCopy(status)`, which
   no page lists in its markup, so the check above never saw it. SUBMITTED, UNKNOWN and CANCELED
   proposals and the console's own UNKNOWN system state rendered as "ps.UNKNOWN" and "sys.UNKNOWN". */
test('every status the API can return has copy, in both languages', () => {
  const { en, zh } = dictionary();
  const status = fs.readFileSync(path.join(root, 'agentic_trading/service/status.py'), 'utf8');
  const members = name => [...status.match(new RegExp(`class ${name}\\(str, Enum\\):([\\s\\S]*?)\\n\\n\\n`))[1]
    .matchAll(/^\s{4}([A-Z_]+) = "/gm)].map(m => m[1]);
  const wanted = [...members('ProposalStatus').map(s => 'ps.' + s),
                  ...members('SystemStatus').map(s => 'sys.' + s),
                  'sys.UNKNOWN'];            // what systemStatus() answers while /health is not live
  assert.ok(wanted.length > 20);
  assert.deepEqual(wanted.filter(k => !en.has(k)), [], 'missing from en');
  assert.deepEqual(wanted.filter(k => !zh.has(k)), [], 'missing from zh');
});

test('every key a script names outright is defined, in both languages', () => {
  const { en, zh } = dictionary();
  const scripts = [];
  const walk = dir => fs.readdirSync(path.join(root, 'ui', dir), { withFileTypes: true }).forEach(entry =>
    entry.isDirectory() ? walk(path.join(dir, entry.name))
      : entry.name.endsWith('.js') && entry.name !== 'i18n.js' && scripts.push(path.join(dir, entry.name)));
  walk('assets');
  const used = new Set();
  for (const file of scripts)
    // a whole key only: t('ux.source.' + key) builds one at run time and is not checked here
    for (const m of uiFile(file).matchAll(/\bt\(\s*'([a-z]\w*(?:\.\w+)+)'/g)) used.add(m[1]);
  assert.ok(used.size > 50, 'the scripts should be naming a lot of copy');
  assert.deepEqual([...used].filter(k => !en.has(k)), [], 'named in a script, missing from en');
  assert.deepEqual([...used].filter(k => !zh.has(k)), [], 'named in a script, missing from zh');
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

test('the product name is Agentic Trading across visible console branding', () => {
  for (const file of pages.filter(file => file !== 'assets/startup.html')) {
    const html = uiFile(file);
    assert.ok(html.includes('Agentic Trading'), `${file} has no product name`);
    assert.ok(!html.includes('Conservative Trading'), `${file} carries an old product name`);
    assert.ok(!html.includes('content="Trading"'), `${file} has the old home-screen name`);
  }
  assert.match(fs.readFileSync(path.join(root, 'ui/assets/startup.html'), 'utf8'), /icons\/mark\.svg.*>Agentic Trading</s);
  assert.match(fs.readFileSync(path.join(root, 'ui/assets/console.js'), 'utf8'), /icons\/mark\.svg.*Agentic Trading/s);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'ui/manifest.webmanifest'), 'utf8'));
  assert.equal(manifest.name, 'Agentic Trading');
  assert.equal(manifest.short_name, 'Agentic Trading');
});

test('the login form never falls back to putting a password in the URL', () => {
  const html = uiFile('login.html');
  const form = html.match(/<form\b[^>]*id="login-form"[^>]*>/)?.[0] || '';
  assert.match(form, /\bmethod="post"/);
  assert.match(form, /\baction="\/auth\/login"/);
  assert.doesNotMatch(form, /\bmethod="get"/);
});

/* ── the research room ────────────────────────────────────────────────
   The office draws one run. These cases pin the line the page must not cross:
   a desk changes only because an event says so, and the decision shown in the
   gate is the deterministic one, never a desk's. */
function labPage(options = {}) {
  const c = context(options.demo === true);
  c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.keys = keys; c.render = render; render(); };
  c.matchMedia = () => ({ matches: true, addEventListener() {} });   // reduced motion: no animation in tests
  c.document.documentElement = { dataset: {} };
  c.requestAnimationFrame = (fn) => fn();
  c.labPixelScene = {
    seats: Array.from({ length: 12 }, (_, i) => ({ x: i, y: i })),
    synced: null,
    sync(data) { this.synced = data; },
    stop() {}, walk: () => 'started',
  };
  if (!options.demo) c.CT.labRoster = options.roster || roster();
  if (!options.demo) c.CT.research = options.research || [];
  if (!options.demo) {
    c.CT.researchJobs = options.jobs || [];
    c.CT.researchRun = options.run || null;
    c.CT.schedule = options.schedule || { analyses_today: 2, daily_budget: 6, next_run_at: '2026-09-15T13:30:00+00:00' };
  }
  c.CT_API = { activeRun: null, watchRun(key) { this.activeRun = key; this.watched = key; },
               fetchJSON: options.fetchJSON || (async () => { throw new Error('no trace expected'); }) };
  c.run('pages/lab.js');
  return c;
}
const ROLES = [
  ['market', 'analysts', 'Market Analyst', 'market', 'market'],
  ['social', 'analysts', 'Sentiment Analyst', 'social', 'social'],
  ['news', 'analysts', 'News Analyst', 'news', 'news'],
  ['fundamentals', 'analysts', 'Fundamentals Analyst', 'fundamentals', 'fundamentals'],
  ['bull', 'research', 'Bull Researcher', 'bull', null],
  ['bear', 'research', 'Bear Researcher', 'bear', null],
  ['manager', 'research', 'Research Manager', 'manager', null],
  ['trader', 'trading', 'Trader', 'trader', null],
  ['aggressive', 'risk', 'Aggressive Analyst', 'aggressive', null],
  ['neutral', 'risk', 'Neutral Analyst', 'neutral', null],
  ['conservative', 'risk', 'Conservative Analyst', 'conservative', null],
  ['portfolio', 'portfolio', 'Portfolio Manager', 'decision', null],
];
const roster = () => ({
  groups: [], call_timeout_seconds: 900,
  roles: ROLES.map(([key, group, node, report, analyst]) => ({ key, group, node, report, analyst })),
  analysts: { etf: ['market', 'news'], stock: ['market', 'social', 'news', 'fundamentals'],
              crypto: ['market', 'social', 'news'] },
});
const deskStates = (c) => Object.fromEntries(
  c.labPixelScene.synced.roles.map((r) => [r.id, r.state]));

test('desks are seated from the run itself, not from a list the page keeps', async () => {
  const trace = {
    run_id: 'r1', ticker: 'VTI', instrument_type: 'etf', analysts: ['market', 'news'],
    policy_action: 'NO_TRADE', hard_risk_verdict: 'NO_ACTION', confidence: 38, elapsed_seconds: 412.6,
    steps: [
      { order: 1, role: 'market', key: 'market', label: 'Market analyst', chars: 6743,
        markdown: '**Interpretation:** Trend is intact, `SMA` stacked.' },
      { order: 2, role: 'news', key: 'news', label: 'News analyst', chars: 4100, markdown: 'Nothing changes the thesis.' },
      { order: 3, role: 'portfolio', key: 'decision', label: 'Portfolio decision', chars: 900, markdown: 'Hold.' },
    ],
  };
  const c = labPage({ research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'etf',
                                   policy_action: 'NO_TRADE', hard_risk_verdict: 'NO_ACTION', confidence: 38,
                                   final_action: 'NO_TRADE', proposal_id: null }],
                       fetchJSON: async () => trace });
  await flush();
  c.render();

  const states = deskStates(c);
  // The two analysts an ETF run never seats, with the engine's own reason rather than a guess.
  assert.equal(states.fundamentals, 'disabled');
  assert.equal(states.social, 'disabled');
  assert.equal(states.market, 'done');
  assert.equal(states.portfolio, 'done');
  // A desk that never reported is waiting, not quietly drawn as finished.
  assert.equal(states.bull, 'waiting');
  assert.match(c.element('#roster').innerHTML, /fundamentals/);
  // The engine's words survive; only the markdown markup around them is dropped.
  const roster = c.element('#roster').innerHTML;
  assert.match(roster, /Interpretation: Trend is intact, SMA stacked\./);
  assert.doesNotMatch(roster, /\*\*|`/);
  assert.match(c.element('#team-count').textContent, /10/);
});

test('a live run lights only the desks its own events name', async () => {
  const c = labPage({
    jobs: [{ run_key: 'live1', ticker: 'VTI', trade_date: '2026-09-10', bucket: 'core',
             instrument_type: 'etf', state: 'RUNNING' }],
    run: { run_key: 'live1', state: 'RUNNING', progress: {
      analysts: ['market', 'news'], started_at: new Date().toISOString(),
      desks: {
        market: { state: 'done', calls: 1, wall_seconds: 28, tools: ['get_indicators'], last_at: new Date().toISOString() },
        bull: { state: 'working', calls: 1, wall_seconds: 0, tools: [], last_at: new Date().toISOString() },
      },
      result: null, events: [], last_event_at: new Date().toISOString(),
    } },
  });
  await flush();

  const states = deskStates(c);
  assert.equal(states.market, 'done');
  assert.equal(states.bull, 'thinking');
  assert.equal(states.bear, 'waiting');       // no event yet, so no claim about it
  assert.equal(c.CT_API.watched, 'live1');    // the run poll follows the run on screen
  // No progress bar and no percentage: the engine reports discrete events only.
  assert.ok(!/%/.test(c.element('#now-caption').textContent));
});

test('a desk is only called stalled once its call outlives the configured timeout', async () => {
  const old = new Date(Date.now() - 1000 * 1000).toISOString();   // beyond 900s
  const recent = new Date(Date.now() - 60 * 1000).toISOString();
  const page = (last) => labPage({
    jobs: [{ run_key: 'l', ticker: 'VTI', trade_date: '2026-09-10', instrument_type: 'etf', state: 'RUNNING' }],
    run: { run_key: 'l', state: 'RUNNING', progress: { analysts: ['market', 'news'], desks: {
      bull: { state: 'working', calls: 1, wall_seconds: 0, tools: [], last_at: last } },
      result: null, events: [], last_event_at: last } },
  });
  assert.equal(deskStates(page(recent)).bull, 'thinking');
  assert.equal(deskStates(page(old)).bull, 'stalled');
});

test('a finished run numbers its stages instead of inventing timestamps', async () => {
  const trace = { run_id: 'r1', ticker: 'VTI', instrument_type: 'etf', analysts: ['market', 'news'],
    policy_action: 'NO_TRADE', hard_risk_verdict: 'NO_ACTION', confidence: 38,
    steps: [{ order: 1, role: 'market', key: 'market', label: 'Market analyst', chars: 6743, markdown: 'Trend is intact.' }] };
  const c = labPage({ research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'etf',
                                   policy_action: 'NO_TRADE', final_action: 'NO_TRADE' }],
                      fetchJSON: async () => trace });
  await flush();
  c.render();

  const feed = c.element('#events').innerHTML;
  assert.match(feed, /#01/);
  assert.doesNotMatch(feed, /\d{2}:\d{2}:\d{2}/, 'a report tree carries an order, not a clock');
});

test('the gate reports the deterministic verdict and offers no approval without a proposal', async () => {
  const trace = { run_id: 'r1', ticker: 'VTI', instrument_type: 'etf', analysts: ['market', 'news'],
    policy_action: 'NO_TRADE', hard_risk_verdict: 'NO_ACTION', confidence: 38, steps: [] };
  const c = labPage({ research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'etf',
                                   policy_action: 'NO_TRADE', hard_risk_verdict: 'NO_ACTION', confidence: 38,
                                   final_action: 'NO_TRADE', proposal_id: null }],
                      fetchJSON: async () => trace });
  await flush();
  c.render();

  const gate = c.element('#gate-result').innerHTML;
  assert.match(gate, /No Trade/);          // C.code() renders the engine's own verdict
  assert.match(gate, /38/);
  assert.equal(c.element('.lab-approval').disabled, true);
});

test('an empty deployment says so instead of drawing a run that never happened', () => {
  const c = labPage({ research: [], jobs: [] });
  const states = deskStates(c);
  assert.ok(Object.values(states).every((s) => ['waiting', 'disabled'].includes(s)));
  assert.equal(c.element('#now-title').textContent, 'lab.noRuns');
  assert.equal(c.element('#symbol').textContent, '—');
});

test('demo mode fills the room from the shipped recording and asks the API for nothing', () => {
  const c = labPage({ demo: true, fetchJSON: async () => { throw new Error('demo must not reach the API'); } });

  const states = deskStates(c);
  // The shipped fixture is a real ETF run, so the two desks it never seated stay empty.
  assert.equal(states.fundamentals, 'disabled');
  assert.equal(states.social, 'disabled');
  assert.equal(states.market, 'done');
  assert.match(c.element('#events').innerHTML, /#01/);
  assert.match(c.element('#gate-result').innerHTML, /No Trade/);
  assert.equal(c.element('.lab-demo-note').hidden, false);
});

test('reports that name a different run are refused, not drawn on this office', async () => {
  const other = { run_id: 'someone-else', ticker: 'BND', instrument_type: 'etf',
                  analysts: ['market', 'news'], policy_action: 'NO_TRADE',
                  steps: [{ order: 1, role: 'market', key: 'market', label: 'Market analyst',
                            chars: 100, markdown: 'Reports from the wrong run.' }] };
  const c = labPage({ research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04',
                                   instrument_type: 'etf', policy_action: 'NO_TRADE' }],
                      fetchJSON: async () => other });
  await flush();
  c.render();

  assert.equal(deskStates(c).market, 'waiting', 'no desk may show another run’s report');
  assert.doesNotMatch(c.element('#roster').innerHTML, /wrong run/);
  assert.equal(c.element('.lab-demo-note').hidden, false);
});

test('unchanged polls and clock ticks preserve the page; errors and new data notify it', async () => {
  const c = context(); let value = [], fails = false, paints = 0;
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : fails ? response({ detail: 'offline' }, 500) : response(value);
  c.run('api.js'); c.document.addEventListener('ct:data', () => { paints++; });
  c.CT_API.start(['proposals']); await flush();
  paints = 0;
  await c.CT_API.readOnce('proposals'); c.intervals.at(-1)();
  assert.equal(paints, 0);
  value = [{ proposal_id: 'new', status: 'WAITING_FOR_APPROVAL' }];
  await c.CT_API.readOnce('proposals'); assert.equal(paints, 1);
  fails = true; await c.CT_API.readOnce('proposals'); assert.equal(paints, 2);
  assert.equal(c.CT_API.canAct(['proposals']), false);
});

test('network reconnect refreshes reads, never replays a write', async () => {
  const c = context(); let reads = 0, posts = 0;
  c.addEventListener = c.document.addEventListener.bind(c.document);
  c.fetch = async (url, options) => {
    if (options.method === 'POST') posts++;
    if (url === '/auth/session') return response({ authenticated: true, csrf_token: 'csrf' });
    reads++; return response([]);
  };
  c.run('api.js'); c.CT_API.start(['proposals']); await flush();
  assert.equal(c.CT_API.canAct(['proposals']), true);
  c.document.dispatchEvent({ type: 'offline' });
  assert.equal(c.CT_API.canAct(['proposals']), false);
  const before = reads;
  c.document.dispatchEvent({ type: 'online' }); await flush();
  assert.ok(reads > before); assert.equal(posts, 0);
  assert.equal(c.CT_API.canAct(['proposals']), true);
  c.document.hidden = true; const hiddenReads = reads;
  c.intervals.forEach(fn => fn()); await flush(); assert.equal(reads, hiddenReads);
  c.document.hidden = false; c.document.dispatchEvent({ type: 'visibilitychange' }); await flush();
  assert.ok(reads > hiddenReads);
});

test('the selected second proposal remains valid when the queue refreshes', async () => {
  const c = context(); const proposals = [{ proposal_id: 'A', version: 'a' }, { proposal_id: 'B', version: 'b' }];
  c.CT.proposals = proposals;
  c.fetch = async url => url === '/proposals' ? response(proposals) : response(proposals[1]);
  c.run('api.js'); c.CT_API.selectProposal('B'); await flush();
  assert.equal(c.CT.proposalDetail.proposal_id, 'B');
  assert.equal(c.CT_API.isLive('proposalDetail'), true);
  await c.CT_API.readOnce('proposals');
  assert.equal(c.CT.proposalDetail.proposal_id, 'B');
});

test('unchanged account markup is not replaced when another resource updates', () => {
  const c = portfolioPage(async () => ({}), { stale: false, snapshot: savedSnapshot, jobs: [] });
  const account = c.element('broker-accounts'); let replacements = 0;
  Object.defineProperty(account, 'innerHTML', { set() { replacements++; } });
  c.render(); c.CT.health.checked_at = 'new time'; c.render();
  assert.equal(replacements, 0);
});

test('home follows active research and clears stale account details when the ledger is withheld', () => {
  const c = homePage(); c.CT_DEMO = false;
  c.CT_API.watchRun = key => { c.CT_API.activeRun = key; };
  c.CT.researchJobs = [{ run_key: '2026-09-15_VTI_test', state: 'RUNNING' }]; c.render();
  assert.equal(c.CT_API.activeRun, '2026-09-15_VTI_test');
  assert.match(c.element('agent-state').textContent, /agent.studying/);
  c.CT.researchJobs = []; c.CT.portfolio = { accounts: {}, buckets: {}, errors: { micro_live: 'unconfirmed' } };
  c.CT.proposals = []; c.render();
  assert.equal(c.CT_API.activeRun, null);
  assert.equal(c.element('hwm-delta').textContent, '—');
  assert.match(c.element('needs-you-slot').innerHTML, /portfolio.html#ledger-notice/);
});

test('holdings research starts once with visible pending feedback', async () => {
  let finish, posts = 0;
  const c = portfolioPage(() => { posts++; return new Promise(resolve => { finish = resolve; }); },
    { stale: false, jobs: [], snapshot: { ...savedSnapshot, accounts: [{ complete: true, positions: [{ symbol: 'VTI', quantity: 1 }] }] } });
  const button = c.element('review-holdings');
  const pending = button.events.click(); await button.events.click();
  assert.equal(posts, 1); assert.equal(button.disabled, true);
  assert.match(c.element('review-status').textContent, /ai.running/);
  finish({ started: ['VTI'], deferred: [] }); await pending;
  assert.equal(button.disabled, false); assert.match(c.element('review-status').textContent, /ai.batch/);
});

function loadingPage(fetch) {
  const c = context();
  c.location.pathname = '/ui/portfolio.html';
  c.element('content').dataset.i18nTitle = 'pf.title';
  c.element('blank-state').remove = function () { this.removed = true; };
  c.fetch = fetch;
  c.run('api.js'); c.run('console.js');
  c.Console.live(['portfolio'], () => { c.draws = (c.draws || 0) + 1; });
  return c;
}

test('slow navigation shows a titled skeleton, reads in parallel and never claims a logout', async () => {
  let session, portfolio, health; const calls = [];
  const c = loadingPage(async url => {
    calls.push(url);
    if (url === '/auth/session') return new Promise(resolve => { session = resolve; });
    if (url === '/health') return new Promise(resolve => { health = resolve; });
    if (url === '/portfolio') return new Promise(resolve => { portfolio = resolve; });
    return response([]);
  });
  assert.ok(calls.includes('/portfolio'), 'page reads must not wait for session confirmation');
  assert.equal(c.element('content').hidden, true);
  assert.match(c.element('blank-state').innerHTML, /pf.title.*ux.loadingPage/);
  assert.ok(!c.element('blank-state').innerHTML.includes('ux.signedOutBody'));
  assert.equal(c.CT_API.writesEnabled, false);
  portfolio(response({ buckets: {}, accounts: {} })); await flush();
  assert.equal(c.element('content').hidden, true, 'verify the owner before restoring page-local drafts');
  assert.equal(c.CT_API.writesEnabled, false, 'an authorized read cannot authorize a write');
  session(response({ authenticated: true, csrf_token: 'csrf' })); await flush();
  assert.equal(c.element('content').hidden, false, 'unrelated slow health must not hold the page');
  assert.ok(c.draws > 0);
  health(response({ status: 'READY' })); await flush();
  assert.equal(c.CT_API.writesEnabled, true);
  assert.equal(c.element('blank-state').removed, true);
});

test('session connection failure offers retry, preserves drafts, and recovers without signing in again', async () => {
  let broken = true;
  const c = loadingPage(async url => {
    if (broken) throw Error('connection lost');
    return response(url === '/auth/session' ? { authenticated: true, csrf_token: 'csrf' }
      : url === '/portfolio' ? { buckets: {}, accounts: {} } : url === '/health' ? { status: 'READY' } : []);
  });
  c.sessionStorage = tabStorage(); c.sessionStorage.setItem('ct.advisor.session.v1', 'draft');
  await flush();
  assert.equal(c.CT_API.state.authChecked, false);
  assert.match(c.element('blank-state').innerHTML, /ux.sessionUnavailable/);
  assert.match(c.element('blank-state').innerHTML, /retry-load/);
  assert.ok(!c.element('conn-bar').innerHTML.includes('login.html'));
  assert.equal(c.sessionStorage.getItem('ct.advisor.session.v1'), 'draft');
  assert.equal(c.CT_API.canAct(['portfolio']), false);
  broken = false; await c.CT_API.refresh(); await flush();
  assert.equal(c.element('content').hidden, false);
  assert.equal(c.CT_API.authenticated, true);
  assert.equal(c.CT_API.writesEnabled, true);
});

test('a confirmed 401 shows sign-in and a late session response cannot undo it', async () => {
  let session;
  const c = loadingPage(async url => url === '/auth/session'
    ? new Promise(resolve => { session = resolve; }) : response({}, 401));
  await flush();
  assert.match(c.element('blank-state').innerHTML, /ux.signedOutBody/);
  assert.equal(c.element('content').hidden, true);
  session(response({ authenticated: true, csrf_token: 'old' })); await flush();
  assert.equal(c.CT_API.authenticated, false);
  assert.equal(c.CT_API.writesEnabled, false);
});

test('failed session recheck keeps loaded content visible while disabling writes', async () => {
  let broken = false;
  const c = loadingPage(async url => {
    if (url === '/auth/session') {
      if (broken) throw Error('timeout');
      return response({ authenticated: true, csrf_token: 'csrf' });
    }
    return response(url === '/portfolio' ? { buckets: {}, accounts: {} } : url === '/health' ? { status: 'READY' } : []);
  });
  await flush(); assert.equal(c.element('content').hidden, false);
  broken = true; await c.CT_API.refresh();
  assert.equal(c.element('content').hidden, false);
  assert.match(c.element('conn-bar').textContent, /ux.sessionUnavailable/);
  assert.equal(c.CT_API.writesEnabled, false);
});

test('primary page shows its real layout while auth and data are pending', async () => {
  const c = context(); let session, portfolio;
  const button = c.element('write-button');
  c.element('content').querySelectorAll = () => [button];
  c.element('blank-state').remove = function () { this.removed = true; };
  c.CT_NAV = { bind() {} };
  c.fetch = async url => url === '/auth/session' ? new Promise(resolve => { session = resolve; })
    : url === '/portfolio' ? new Promise(resolve => { portfolio = resolve; }) : response([]);
  c.run('api.js'); c.run('console.js');
  let draws = 0;
  c.Console.live(['portfolio'], () => { draws++; });
  assert.equal(c.element('content').hidden, false);
  assert.equal(c.element('blank-state').removed, true);
  assert.equal(button.disabled, true);
  assert.equal(draws, 0, 'do not render empty data as zero balances');
  portfolio(response({ accounts: {}, buckets: {} })); await flush();
  assert.equal(draws, 0, 'drafts still wait for verified identity');
  session(response({ authenticated: true, csrf_token: 'csrf' })); await flush();
  assert.ok(draws > 0);
  assert.equal(button.disabled, false);
});

test('switching primary tabs reuses authenticated reads and keeps one set of polling timers', async () => {
  const c = context(), calls = [], activeTimers = new Set();
  let timerId = 0;
  c.setInterval = () => { const id = ++timerId; activeTimers.add(id); return id; };
  c.clearInterval = id => activeTimers.delete(id);
  c.fetch = async url => {
    calls.push(url);
    return response(url === '/auth/session' ? { authenticated: true, csrf_token: 'csrf' } : {});
  };
  c.run('api.js'); c.CT_API.start(['health', 'portfolio']); await flush();
  for (let i = 0; i < 20; i++) {
    c.CT_API.start(i % 2 ? ['health', 'portfolio'] : ['health', 'advisor']);
    await flush();
    assert.equal(c.CT_API.state.authPending, false);
    assert.equal(activeTimers.size, 3);
  }
  for (const path of ['/auth/session', '/health', '/portfolio', '/advisor']) {
    assert.equal(calls.filter(x => x === path).length, 1, path);
  }
});

test('a late account read and another tab cannot restore an invalidated session', async () => {
  const c = context(); let portfolio;
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : url === '/portfolio' ? new Promise(resolve => { portfolio = resolve; })
    : response({}, 401);
  c.run('api.js');
  c.CT_API.start(['portfolio']); await flush();
  await c.CT_API.readOnce('health');
  portfolio(response({ accounts: { private: { equity_usd: 123 } }, buckets: {} })); await flush();
  assert.equal(c.CT.portfolio.accounts.private, undefined);
  c.CT_API.start(['advisor']);
  assert.equal(c.CT_API.state.authPending, false);
  assert.equal(c.CT_API.authenticated, false);
  assert.equal(c.CT_API.writesEnabled, false);
});

function startupPage(storage = tabStorage()) {
  const c = context(), timers = new Map(); let nextTimer = 0;
  c.sessionStorage = storage;
  c.setTimeout = (fn, ms) => { timers.set(++nextTimer, { fn, ms }); return nextTimer; };
  c.clearTimeout = id => timers.delete(id);
  c.fireTimer = ms => {
    for (const [id, timer] of [...timers]) if (timer.ms === ms) { timers.delete(id); timer.fn(); }
  };
  c.hasTimer = ms => [...timers.values()].some(t => t.ms === ms);
  c.document.documentElement = { removeAttribute: () => { c.revealed = true; } };
  c.document.querySelector = selector => selector === '#content:not([hidden])' ? c.element('content') : null;
  c.matchMedia = () => ({ matches: false });
  c.element('startup').focus = () => {};
  c.element('startup').blur = () => { c.blurred = true; };
  c.element('startup').contains = () => true;
  c.element('startup').remove = () => { c.removed = true; };
  c.element('content').focus = () => { c.contentFocused = true; };
  c.element('startup-actions').hidden = true;
  const copies = ['preparing', 'slow', 'failed'].map(key => ({ dataset: { i18n: 'startup.' + key } }));
  c.element('startup-message').querySelectorAll = () => copies;
  c.message = () => copies.find(x => !x.hidden)?.dataset.i18n;
  c.CT_API = { authenticated: false, refresh: async () => {} };
  c.emitStatus = status => c.document.dispatchEvent({ type: 'ct:page-status', detail: { status } });
  c.run('startup.js');
  return c;
}

test('brand introduction appears once per tab session, including failed loads and reloads', () => {
  const storage = tabStorage();
  const first = startupPage(storage);
  assert.equal(first.element('startup').classList.contains('is-first'), true);
  const next = startupPage(storage);
  assert.equal(next.element('startup').classList.contains('is-first'), false);
  next.emitStatus('error');
  assert.equal(next.element('startup-actions').hidden, false, 'compact loads retain recovery controls');
  assert.equal(startupPage().element('startup').classList.contains('is-first'), true, 'a new session gets the introduction');
});

test('startup waits for authenticated rendering, then stays dismissed through later loads', () => {
  const c = startupPage();
  c.emitStatus('ready');
  assert.equal(c.revealed, undefined, 'an empty more page must still wait for identity');
  c.CT_API.authenticated = true;
  c.emitStatus('ready');
  assert.equal(c.revealed, true);
  assert.equal(c.contentFocused, true, 'startup focus moves into content, not the selected navigation tab');
  assert.equal(c.hasTimer(10000), false, 'readiness cancels the watchdog, without a minimum delay');
  c.fireTimer(180);
  assert.equal(c.removed, true);
  c.emitStatus('loading'); c.emitStatus('error'); c.fireTimer(10000);
  assert.equal(c.element('startup-actions').hidden, true);
});

test('slow startup offers an escape and never blocks navigation again after continuing', () => {
  const c = startupPage(); c.fireTimer(10000);
  assert.equal(c.message(), 'startup.slow');
  assert.equal(c.element('startup-actions').hidden, false);
  c.element('startup-continue').events.click();
  assert.equal(c.revealed, true);
  c.fireTimer(180);
  c.emitStatus('loading');
  assert.equal(c.removed, true);
});

test('startup retry reloads when the page script never initialized', async () => {
  const c = startupPage(); let reloads = 0;
  c.location.reload = () => { reloads++; };
  c.fireTimer(10000);
  await c.element('startup-retry').events.click();
  assert.equal(reloads, 1);
});

test('startup failure allows one read retry and closes on recovery or confirmed logout', async () => {
  const c = startupPage(); let complete, calls = 0;
  c.CT_API.refresh = () => { calls++; return new Promise(resolve => { complete = resolve; }); };
  c.emitStatus('error');
  assert.equal(c.message(), 'startup.failed');
  const retry = c.element('startup-retry');
  const pending = retry.events.click(); await retry.events.click();
  assert.equal(calls, 1);
  assert.equal(c.message(), 'startup.preparing');
  assert.equal(retry.disabled, true);
  c.CT_API.authenticated = true; c.emitStatus('ready'); complete(); await pending;
  assert.equal(c.revealed, true);
  const signedOut = startupPage(); signedOut.emitStatus('signedOut');
  assert.equal(signedOut.revealed, true, 'sign-in must never be trapped behind the curtain');
});

/* Acting on a proposal usually closes it, and renderDetail hides the proposal and live-condition
   sections as soon as the queue is empty. The outcome lived inside the proposal section, so an
   approval's fill or refusal rendered and vanished within a second. */
test('the outcome of an action is not inside a section that hides when the queue empties', () => {
  const html = fs.readFileSync(path.join(root, 'ui/approvals.html'), 'utf8');
  const sections = html.split('<section').slice(1);
  const holder = sections.find(s => s.includes('id="result-slot"'));
  assert.ok(holder, 'the result slot is on the page');
  assert.ok(!holder.includes('data-od-id="proposal-detail"'));
  assert.ok(!holder.includes('id="live-conditions"'));
  assert.ok(!holder.split('>')[0].includes('hidden'));
});

function serviceWorker(fetchImpl) {
  const listeners = {}, calls = [];
  const self = { addEventListener: (type, fn) => { listeners[type] = fn; },
    registration: { scope: 'https://console.test/ui/',
      pushManager: { subscribe: async options => ({ options, toJSON: () => ({ endpoint: 'https://push.test/new', keys: {} }) }) } } };
  const ctx = vm.createContext({ self, atob, Uint8Array, JSON, Error, console: { warn: (...a) => calls.push(['warn', a]) },
    fetch: async (url, init = {}) => { calls.push([url, init]); return fetchImpl(url, init); } });
  vm.runInContext(fs.readFileSync(path.join(root, 'ui/sw.js'), 'utf8'), ctx, { filename: 'sw.js' });
  return { listeners, calls };
}

test('a rotated push subscription is recorded with the session CSRF token', async () => {
  const { listeners, calls } = serviceWorker(async url => url === '../auth/session'
    ? response({ authenticated: true, csrf_token: 'session-token' }) : response({ ok: true }));
  let done;
  listeners.pushsubscriptionchange({ oldSubscription: { options: { userVisibleOnly: true, applicationServerKey: new Uint8Array([4]) } },
    waitUntil: promise => { done = promise; } });
  await done;
  const post = calls.find(([url]) => url === '../notifications/subscribe');
  assert.ok(post, 'the renewal reached the API');
  assert.equal(post[1].method, 'POST');
  assert.equal(post[1].headers['X-CSRF-Token'], 'session-token');
  assert.equal(JSON.parse(post[1].body).renewed, true);
  assert.ok(!calls.some(([kind]) => kind === 'warn'));
});

test('a renewal the API refuses is reported, not swallowed', async () => {
  const { listeners, calls } = serviceWorker(async url => url === '../auth/session'
    ? response({ authenticated: true, csrf_token: 'session-token' }) : response({ detail: 'no' }, 403));
  let done;
  listeners.pushsubscriptionchange({ oldSubscription: { options: { userVisibleOnly: true, applicationServerKey: new Uint8Array([4]) } },
    waitUntil: promise => { done = promise; } });
  await done;
  assert.ok(calls.some(([kind, args]) => kind === 'warn' && String(args[1]).includes('403')));
});

test('the stale-ledger control runs the ledger confirmation and reports a refusal', async () => {
  const c = context(); c.run('console.js');
  const sent = [], alerts = [], read = [];
  c.alert = message => alerts.push(message);
  c.CT_API = { post: async (url, body) => { sent.push([url, body]);
    return { confirmed: false, ledgers: [{ ledger: 'micro_live', error: 'PortfolioError: cash mismatch' }] }; },
  readOnce: async key => { read.push(key); } };
  const button = { disabled: false };
  await c.Console.confirmLedger(button);
  assert.equal(JSON.stringify(sent), JSON.stringify([['/portfolio/confirm', { ledger: 'all' }]]));
  assert.match(alerts[0], /pf.confirmRefused.*cash mismatch/);
  assert.deepEqual(read, ['portfolio']);
  assert.equal(button.disabled, false);
});

test('a sign-in that fails with a non-JSON answer says so instead of showing a parse error', async () => {
  const c = context();
  c.I18N.applyStatic = () => {};
  c.element('login-form').elements = { username: { value: 'owner' }, password: { value: 'pw' } };
  let assigned = null;
  c.location.assign = url => { assigned = url; };
  c.fetch = async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected token <'); } });
  c.run('login.js');
  await c.element('login-form').events.submit({ preventDefault() {} });
  assert.equal(c.element('login-result').textContent, 'login.failed 502');
  assert.equal(c.element('login-submit').disabled, false);
  assert.equal(assigned, null);
});
