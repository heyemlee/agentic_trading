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
  c.location.search = options.search || '';
  c.CT.advisor = { scope: 'personal', account_scope_kind: 'all_personal', account_id: null, notes: { text: 'Saved plan', revision: 1 }, jobs: [], decisions: [],
    snapshot: { accounts: [], candidates: [{ symbol: 'VTI', sources: ['watchlists:<script>'] }] } };
  Object.defineProperty(c.CT, 'assistant', { get: () => c.CT.advisor });
  c.CT.accounts = { accounts: ['aaaaaaaaaaaaaaaaaaaa', 'bbbbbbbbbbbbbbbbbbbb'].map(account_id => ({ scope: 'personal', account_scope_kind: 'single', account_id })) };
  c.CT_API = { isLive: () => true, matchesAssistant: () => true, selectAssistant: f => {
    Object.assign(c.CT.advisor, { scope: f.scope, account_scope_kind: f.scope === 'agentic' || f.account ? 'single' : 'all_personal', account_id: f.scope === 'agentic' ? 'micro_live' : f.account || null });
  }, writesEnabled: true, authenticated: true, username: options.owner || 'owner', post, readOnce: async () => {},
    fetchJSON: options.fetchJSON || (async url => ({ conversation_id: url.split('/').at(-1), messages: [] })) };
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

test('personal report research stays read-only and preserves its account', async () => {
  const sent = [];
  const c = advisorPage(async (url, body) => { sent.push({ url, body }); return {}; });
  c.CT.advisor.decisions = [{ run_id: 'personal-run', ticker: 'AAPL', account_id: '1234567890abcdef1234',
    bucket: 'advisory', policy: { proposal: {} } }];
  c.render();
  c.document.dispatchEvent({ type: 'click', target: { closest: selector => selector === '[data-decision]'
    ? { dataset: { decision: 'personal-run' } } : null } });
  const html = c.element('decision-detail').innerHTML;
  assert.ok(!html.includes('research.html?ticker='));
  assert.match(html, /data-research="AAPL" data-account="1234567890abcdef1234"/);
  assert.equal(sent.length, 0, 'opening a report does not start research');
  c.document.dispatchEvent({ type: 'click', target: { closest: selector => selector === '[data-research], [data-cancel]'
    ? { dataset: { research: 'AAPL', account: '1234567890abcdef1234' } } : null } });
  await flush();
  assert.equal(sent.length, 1); assert.equal(sent[0].url, '/advisor/jobs');
  assert.equal(sent[0].body.kind, 'analysis');
  assert.equal(sent[0].body.account_id, '1234567890abcdef1234');
});

test('research confidence uses each recorded threshold and preserves missing scores', () => {
  const c = context(true); c.run('console.js');
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { render(); };
  c.element('[data-od-id="confidence-chart"]').before = () => {};
  c.element('f-bucket').options = []; c.element('f-bucket').value = 'micro_live';
  c.CT_API = { activeRun: null, watchRun: () => {}, readOnce: async () => {}, state: { resources: {} },
    isLive: () => true, matchesResearch: () => true, selectResearch: () => { c.CT.researchHistory = c.CT.research; } };
  c.CT.research = [
    { run_id: 'threshold', bucket: 'micro_live', ticker: 'VTI', confidence: 70, applicable_floor: 80 },
    { run_id: 'legacy', bucket: 'micro_live', ticker: 'BND', confidence: 40 },
    { run_id: 'missing', bucket: 'micro_live', ticker: 'VT', confidence: null, applicable_floor: null },
  ];
  c.run('pages/research.js');
  const html = c.element('conf-chart').innerHTML;
  assert.match(html, /left:80%/); assert.ok(!html.includes('left:50%'));
  assert.equal((html.match(/meter-fill is-warn/g) || []).length, 1);
  assert.equal((html.match(/meter-track/g) || []).length, 2, 'a missing score has no zero-width meter');
  assert.match(html, /rs.floorUnavailable/);
  assert.ok(!c.element('panel-runs-all').innerHTML.includes('null'));
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

const discoverSymbols = c => [...c.element('candidate-list').innerHTML.matchAll(/class="dc-symbol">([^<]+)</g)].map(m => m[1]);
const pickFilter = (c, filter) => c.element('discover-filters').events.click({
  target: { closest: selector => selector === '[data-filter]' ? { dataset: { filter } } : null } });

test('Discover filters the pool by where a symbol came from, and a search looks through all of it', () => {
  const c = advisorPage(async () => ({}));
  c.CT.advisor.snapshot.candidates = [{ symbol: 'AMD', sources: ['watchlists:Semis', 'scans:Momentum'] },
    { symbol: 'VTI', sources: ['profile:us_broad_equity'] }, { symbol: 'AVGO', sources: ['watchlists:Semis'] }];
  c.render();
  const chips = c.element('discover-filters').innerHTML;
  assert.match(chips, /data-filter="all" aria-pressed="true">dc.all<span class="dc-chip-n">3</);
  assert.match(chips, /data-filter="watchlists" aria-pressed="false">dc.kind.watchlists<span class="dc-chip-n">2</);
  assert.ok(!chips.includes('data-filter="other"'), 'a kind nothing came from offers no filter');
  pickFilter(c, 'scans');
  assert.deepEqual(discoverSymbols(c), ['AMD']);
  c.element('symbol-input').value = 'v'; c.element('symbol-input').events.input();
  assert.deepEqual(discoverSymbols(c), ['VTI', 'AVGO'], 'a search ignores the filter and puts a prefix first');
  assert.ok(!c.element('discover-filters').innerHTML.includes('aria-pressed="true"'));
  c.element('symbol-input').value = 'zzz'; c.element('symbol-input').events.input();
  assert.match(c.element('candidate-list').innerHTML, /dc.noMatch ZZZ/);
  c.element('symbol-input').events.keydown({ key: 'Escape' });
  assert.deepEqual(discoverSymbols(c), ['AMD'], 'clearing the search returns to the chosen source');
});

test('a candidate under review offers no second review, and says what is held and was last decided', () => {
  const c = advisorPage(async () => ({}));
  Object.assign(c.CT.advisor, {
    snapshot: { accounts: [{ id: 'a'.repeat(20), positions: [{ symbol: 'NVDA', quantity: 2 }] }],
      candidates: [{ symbol: 'NVDA', sources: ['watchlists:Semis'] }, { symbol: 'AVGO', sources: ['watchlists:Semis'] }] },
    jobs: [{ run_key: 'r1', ticker: 'AVGO', state: 'RUNNING', task: { kind: 'analysis' } }],
    decisions: [{ run_id: 'd2', ticker: 'NVDA', action: 'HOLD', trade_date: '2026-09-03', bucket: 'advisory', policy: { proposal: {} } },
      { run_id: 'd1', ticker: 'NVDA', action: 'BUY', trade_date: '2026-08-01', bucket: 'advisory', policy: { proposal: {} } }] });
  c.render();
  const html = c.element('candidate-list').innerHTML;
  assert.ok(!html.includes('data-research="AVGO"'), 'a running review is not offered again');
  assert.match(html, /dc-running.*dc.researching/);
  assert.match(html, /data-research="NVDA"/);
  assert.match(html, /dc-row is-held/);
  assert.match(html, /dc.last Hold Sep 3/, 'the newest verdict, with its date');
  assert.ok(!html.includes('Buy'));
  c.CT.advisor.jobs[0].state = 'COMPLETE'; c.render();
  assert.match(c.element('candidate-list').innerHTML, /data-research="AVGO"/);
});

test('Discover refuses a malformed symbol before asking, and reports a queued review where it was asked for', async () => {
  const sent = [];
  const c = advisorPage(async (url, body) => { sent.push(body); return { run_key: 'advisor_x' }; });
  c.element('view-discover').events.click();
  const submit = () => c.element('symbol-form').events.submit({ preventDefault() {} });
  c.element('symbol-input').value = '1bad!'; await submit();
  assert.equal(sent.length, 0);
  assert.equal(c.element('discover-status').textContent, 'dc.badSymbol');
  c.element('symbol-input').value = 'brk.b'; c.element('symbol-input').events.input(); await submit();
  assert.deepEqual([sent.length, sent[0].kind, sent[0].ticker], [1, 'analysis', 'BRK.B']);
  assert.equal(c.element('discover-status').textContent, 'dc.queued BRK.B');
  assert.equal(c.element('symbol-input').value, '', 'a queued symbol leaves the field');
});

test('a long candidate pool shows a page at a time', () => {
  const c = advisorPage(async () => ({}));
  c.CT.advisor.snapshot.candidates = Array.from({ length: 30 }, (_, i) => ({ symbol: 'S' + i, sources: ['profile:core'] }));
  c.render();
  assert.equal(discoverSymbols(c).length, 24);
  assert.equal(c.element('candidate-more').hidden, false);
  assert.equal(c.element('candidate-more').textContent, 'dc.more 6');
  c.element('candidate-more').events.click();
  assert.equal(discoverSymbols(c).length, 30);
  assert.equal(c.element('candidate-more').hidden, true);
  assert.equal(c.element('shortlist').disabled, false);
  c.CT.advisor.snapshot.candidates = []; c.render();
  assert.equal(c.element('shortlist').disabled, true, 'an empty pool has nothing to rank, and a reply would still be spent');
  assert.match(c.element('candidate-list').innerHTML, /dc.noSnapshot/);
});

test('the demo assistant is the one advisor fixture, narrowed to an account the way the service narrows it', () => {
  const c = context(true); c.run('api.js');
  const A = 'a'.repeat(20), B = 'b'.repeat(20);
  c.CT.advisor = { scope: 'personal', account_scope_kind: 'all_personal', stale: false, coverage: 'demo',
    snapshot: { accounts: [{ id: A, positions: [] }, { id: B, positions: [] }], candidates: [{ symbol: 'VTI', sources: ['profile:core'] }] },
    jobs: [{ run_key: 'mine', task: { kind: 'analysis', account_id: A, account_scope_kind: 'single' } },
      { run_key: 'all', task: { kind: 'analysis', account_id: null, account_scope_kind: 'all_personal', account_ids: [A, B] } },
      { run_key: 'review', task: { kind: 'portfolio_review', account_id: B, account_scope_kind: 'single' } }],
    decisions: [{ run_id: 'da', account_id: A, account_scope_kind: 'single' }, { run_id: 'db', account_id: B, account_scope_kind: 'single' }],
    latest_review: { run_key: 'review' } };
  c.CT_API.selectAssistant({ scope: 'personal', account: A });
  const scoped = c.CT.assistant;
  assert.equal(c.CT_API.matchesAssistant(scoped), true);
  assert.deepEqual(scoped.snapshot.accounts.map(a => a.id), [A]);
  assert.deepEqual(scoped.snapshot.candidates.map(x => x.symbol), ['VTI'], 'the candidate pool belongs to no one account');
  assert.deepEqual(scoped.jobs.map(j => j.run_key), ['mine', 'all']);
  assert.deepEqual(scoped.decisions.map(d => d.run_id), ['da']);
  assert.equal(scoped.latest_review, null, "another account's review stays with it");
  c.CT_API.selectAssistant({ scope: 'agentic' });
  assert.equal(c.CT.assistant.snapshot, undefined, 'Agentic carries no personal snapshot');
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
  assert.ok(optional.includes('microLive'), 'the Agentic account is read live from the venue');
  assert.match(c.element('broker-accounts').innerHTML, /Account &lt;img&gt;/);
  assert.match(c.element('broker-accounts').innerHTML, /5,000/);
  assert.match(c.element('broker-accounts').innerHTML, /ux.incompleteAccount/);
  assert.match(c.element('broker-stamp').textContent, /ai.stale/);
  assert.equal(c.element('pf-total').textContent, c.Console.usd(c.CT.portfolio.buckets.micro_live.equity_usd));
  c.CT.advisor = {}; c.render();
  assert.equal(c.element('broker-accounts').innerHTML, '');
  assert.equal(c.element('broker-stamp').textContent, 'ai.empty');
});

test('portfolio account cards switch the entire visible view without mixing balances', async () => {
  const advisor = { stale: true, jobs: [], snapshot: { fetched_at: '2026-09-09T12:00:00Z', accounts: [
    { id: 'personal-1', label: 'Personal', equity_usd: 5052, cash_usd: 3737, complete: true, positions: [] }
  ] } };
  const open = scope => {
    const c = context(true); c.location.search = '?scope=' + scope + '&demo=1'; c.run('console.js');
    c.Console.mount = () => {};
    c.Console.live = (keys, render) => { c.render = render; render(); };
    c.CT.advisor = advisor;
    c.posts = [];
    c.CT_API = { isLive: () => true, writesEnabled: true, readOnce: async () => {},
      post: async path => { c.posts.push(path); return { run_key: 'refresh' }; } };
    c.run('pages/portfolio.js');
    return c;
  };
  const personal = open('robinhood');
  assert.equal(personal.element('robinhood-view').hidden, false);
  assert.equal(personal.element('agentic-view').hidden, true);
  assert.match(personal.element('portfolio-switch').innerHTML, /portfolio-switch-robinhood[^>]*aria-current="page"/);
  assert.match(personal.element('broker-accounts').innerHTML, /5,052/);
  const agentic = open('agentic'); await flush();
  assert.equal(agentic.element('robinhood-view').hidden, true);
  assert.equal(agentic.element('agentic-view').hidden, false);
  assert.match(agentic.element('portfolio-switch').innerHTML, /portfolio-switch-agentic[^>]*aria-current="page"/);
  assert.equal(agentic.element('pf-total').textContent, agentic.Console.usd(agentic.CT.portfolio.accounts.micro_live.equity_usd));
  assert.doesNotMatch(agentic.element('account-cards').innerHTML, /5,052|conceptual/);
  assert.deepEqual(agentic.posts, [], 'opening the Agentic view must not refresh personal holdings');
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

/* Tapping a Robinhood holding used to open the Research page, which with no scope in the link
   came up in the Agentic scope with the strategy form prefilled: a personal holding one tap from
   a real-money candidate run, and none of its own analyses on the screen. It has a view of its
   own now, drawn from the /advisor state the page already polls. */
const holdingAccount = 'a'.repeat(20), otherAccount = 'b'.repeat(20);
function holdingPage(advisor, { post, writes = true,
                                search = '?scope=robinhood&account=' + holdingAccount + '&ticker=NVDA' } = {}) {
  const c = context(); c.location.search = search; c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.posts = []; c.reread = 0;
  c.CT_API = { isLive: () => true, writesEnabled: writes, readOnce: async () => { c.reread++; },
    post: post || (async (path, body) => { c.posts.push([path, body]); return { run_key: 'advisor_new' }; }) };
  c.CT.advisor = advisor;
  c.run('pages/portfolio.js');
  return c;
}
const nvda = { symbol: 'NVDA', quantity: 40, average_cost: 98.5, price: 131.4, market_value_usd: 5256,
               unrealized_pct: 33.401, quote_at: '2026-09-04T20:00:00Z', weight_pct: 11.6 };
const holdingSnapshot = (positions = [nvda], complete = true) => ({ fetched_at: '2026-09-04T20:31:00Z', accounts: [
  { id: holdingAccount, label: '••••1111', complete, equity_usd: 45249.5, cash_usd: 3737, positions },
  { id: otherAccount, label: '••••2222', complete: true, equity_usd: 12480.4, cash_usd: 612.4,
    positions: [{ ...nvda, quantity: 1 }] }] });
const decided = (run_id, account_id, kind, action, created_at, thesis) => ({ run_id, ticker: 'NVDA', account_id,
  account_scope_kind: kind, trade_date: created_at.slice(0, 10), created_at, action,
  policy: { confidence: 54, proposal: { investment_thesis: thesis, entry_logic: 'Entry', exit_logic: 'Exit',
                                        primary_risks: ['Risk'], missing_information: [] } } });

test('a Robinhood holding opens its own view, never the strategy research form', () => {
  const c = holdingPage({ stale: false, jobs: [], decisions: [],
    snapshot: { ...holdingSnapshot(), errors: ['Scan <b>: only the first candidate page was read.'] } }, { search: '' });
  const list = c.element('broker-accounts').innerHTML;
  assert.match(list, /href="portfolio\.html\?scope=robinhood&amp;account=a{20}&amp;ticker=NVDA"/);
  assert.doesNotMatch(list, /research\.html/);
  assert.match(list, /<details class="pf-card pf-more pf-notes"><summary>pf\.readNotes 1<\/summary>/,
               'what the read could not do is kept, one tap away');
  assert.match(list, /Scan &lt;b&gt;: only the first/);
  assert.equal(c.element('pf-overview').hidden, false);
  assert.equal(c.element('holding-view').hidden, true);
});

test('a holding shows its position and only the analyses bound to its account', () => {
  const c = holdingPage({ stale: false, jobs: [], snapshot: holdingSnapshot(), decisions: [
    decided('mine_new', holdingAccount, 'single', 'HOLD', '2026-09-04T01:00:00Z', 'Hold <b>this</b>'),
    decided('other_account', otherAccount, 'single', 'SELL', '2026-09-04T02:00:00Z', 'Another account'),
    decided('unbound', null, 'unknown', 'SELL', '2026-09-04T03:00:00Z', 'Never said which account'),
    decided('everyone', null, 'all_personal', 'NO_TRADE', '2026-09-02T01:00:00Z', 'All accounts'),
    decided('mine_old', holdingAccount, 'single', 'ADD', '2026-08-20T01:00:00Z', 'Older') ] });
  assert.equal(c.element('holding-view').hidden, false);
  assert.equal(c.element('pf-overview').hidden, true);
  assert.equal(c.element('holding-symbol').textContent, 'NVDA');
  assert.equal(c.element('holding-value').textContent, c.Console.usd(5256));
  assert.match(c.element('holding-return').innerHTML, /\+\$1,316\.00 \(\+33\.40%\)/);
  assert.match(c.element('holding-stats').innerHTML, /\$3,940\.00/, 'cost basis from average cost and quantity');
  const latest = c.element('holding-analysis').innerHTML, earlier = c.element('holding-history').innerHTML;
  assert.match(latest, /data-od-id="analysis-mine_new"/);
  assert.match(latest, /Hold &lt;b&gt;this&lt;\/b&gt;/, 'the engine text is escaped, never markup');
  assert.match(earlier, /run=everyone/);
  assert.match(earlier, /run=mine_old/);
  assert.doesNotMatch(latest + earlier, /other_account|unbound/);
  assert.equal(c.element('holding-robinhood').href, 'https://robinhood.com/stocks/NVDA');
  assert.equal(c.element('holding-analyze').disabled, false);
});

test('analysing a holding sends the assistant request bound to its account, once per tap', async () => {
  let fail = true; const sent = [];
  const c = holdingPage({ stale: false, jobs: [], decisions: [], snapshot: holdingSnapshot() }, {
    post: async (path, body) => { sent.push([path, body]); if (fail) throw Error('network lost'); return { run_key: 'advisor_new' }; } });
  c.element('holding-analyze').events.click();
  c.element('holding-analyze').events.click(); await flush();
  assert.equal(sent.length, 1, 'a second tap while the first is in flight sends nothing');
  assert.equal(sent[0][0], '/advisor/jobs');
  assert.equal(JSON.stringify({ ...sent[0][1], request_id: 'id' }), JSON.stringify({ kind: 'analysis', scope: 'personal',
    ticker: 'NVDA', account_id: holdingAccount, request_id: 'id', language: 'en' }));
  assert.match(c.element('holding-analyze-note').textContent, /hd.requestFailed network lost/);
  fail = false;
  c.element('holding-analyze').events.click(); await flush();
  assert.equal(sent.length, 2);
  assert.equal(sent[1][1].request_id, sent[0][1].request_id, 'an explicit retry is the same request');
  assert.match(c.element('holding-analyze-note').textContent, /hd.requested/);
  assert.equal(c.reread, 1, 'the queue is read again so the run shows up');
});

test('an analysis in flight holds the button and can be stopped; another account\'s run cannot', async () => {
  const running = { run_key: 'advisor_running', ticker: 'NVDA', state: 'RUNNING', created_at: '2026-09-04T20:40:00Z',
                    task: { kind: 'analysis', account_scope_kind: 'single', account_id: holdingAccount } };
  const c = holdingPage({ stale: false, decisions: [], snapshot: holdingSnapshot(), jobs: [
    { ...running, run_key: 'elsewhere', task: { ...running.task, account_id: otherAccount } }, running] });
  assert.equal(c.element('holding-analyze').disabled, true);
  assert.match(c.element('holding-job').innerHTML, /data-stop="advisor_running"/);
  assert.doesNotMatch(c.element('holding-job').innerHTML, /elsewhere/);
  await c.element('holding-job').events.click({ target: { closest: () => ({ getAttribute: () => 'advisor_running' }) } });
  await flush();
  assert.deepEqual(c.posts.map(([path]) => path), ['/research/advisor_running/cancel']);
});

test('a failed run is reported until a newer analysis replaces it', () => {
  const failed = { run_key: 'advisor_failed', ticker: 'NVDA', state: 'FAILED', error: 'Claude limit <reached>',
                   created_at: '2026-09-05T01:00:00Z', task: { kind: 'analysis', account_scope_kind: 'single', account_id: holdingAccount } };
  const c = holdingPage({ stale: false, snapshot: holdingSnapshot(), jobs: [failed],
    decisions: [decided('mine', holdingAccount, 'single', 'HOLD', '2026-09-04T01:00:00Z', 'Older result')] });
  assert.match(c.element('holding-job').innerHTML, /Claude limit &lt;reached&gt;/);
  c.CT.advisor.decisions.unshift(decided('newer', holdingAccount, 'single', 'HOLD', '2026-09-05T02:00:00Z', 'Newer'));
  c.render();
  assert.equal(c.element('holding-job').innerHTML, '');
});

test('a holding that left the snapshot keeps its history and offers no analysis', () => {
  const c = holdingPage({ stale: false, jobs: [], snapshot: holdingSnapshot([]),
    decisions: [decided('mine', holdingAccount, 'single', 'HOLD', '2026-09-04T01:00:00Z', 'Kept')] });
  assert.equal(c.element('holding-missing').hidden, false);
  assert.equal(c.element('holding-stats').hidden, true);
  assert.equal(c.element('holding-value').hidden, true, 'no figure is drawn for a position that is not there');
  assert.equal(c.element('holding-analyze').hidden, true);
  assert.match(c.element('holding-analysis').innerHTML, /Kept/);
});

test('an incomplete account, or a session that cannot write, cannot start an analysis', () => {
  const incomplete = holdingPage({ stale: false, jobs: [], decisions: [], snapshot: holdingSnapshot([nvda], false) });
  assert.equal(incomplete.element('holding-analyze').disabled, true);
  assert.match(incomplete.element('holding-analyze-note').textContent, /hd.incomplete/);
  const readOnly = holdingPage({ stale: false, jobs: [], decisions: [], snapshot: holdingSnapshot() }, { writes: false });
  assert.equal(readOnly.element('holding-analyze').disabled, true);
  assert.match(readOnly.element('holding-analyze-note').textContent, /hd.unavailable/);
  readOnly.element('holding-analyze').events.click();
  assert.equal(readOnly.posts.length, 0);
});

test('the portfolio review speaks about a holding only when it covered that account', () => {
  const review = { account_scope_kind: 'single', account_id: otherAccount, analysed_at: '2026-09-04T02:00:00Z',
                   run_key: 'advisor_review', priorities: [{ symbol: 'NVDA', recommendation: 'consider_change',
                                                             rationale: 'Trim it', missing: [] }] };
  const other = holdingPage({ stale: false, jobs: [], decisions: [], snapshot: holdingSnapshot(), latest_review: review });
  assert.equal(other.element('holding-review-block').hidden, true);
  const covering = holdingPage({ stale: false, jobs: [], decisions: [], snapshot: holdingSnapshot(),
    latest_review: { ...review, account_scope_kind: 'all_personal', account_id: null, account_ids: [holdingAccount, otherAccount] } });
  assert.equal(covering.element('holding-review-block').hidden, false);
  assert.match(covering.element('holding-review').innerHTML, /Trim it/);
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

function ledgerPage(post, portfolio, fetchJSON) {
  const c = context(); c.run('console.js');
  c.location.search = '?scope=agentic';
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { isLive: () => true, writesEnabled: true, readOnce: async () => { c.reread = true; }, post,
               fetchJSON: fetchJSON || (async () => ({})) };
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
  assert.equal(sent[0][1].ledger, 'micro_live');
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

/* The Agentic account is real money, so its balance is the venue's own figure, read live; the
   ledger the risk rules run on follows with its own date. On 2026-09-23 a vendor's missing bar
   held the ledger back for a day, and the page showed nothing of an account that read fine. */
const readableLedger = { ...heldBack, errors: {},
  buckets: { micro_live: { equity_usd: 100, allocation_usd: 100, cash_usd: 100, positions_value_usd: 0,
                           positions: [], position_count: 0, sleeves_pct: {}, account: 'micro_live',
                           is_real_money: true, venue: 'robinhood' } } };
const liveVenue = () => ({ connected: true, checked_at: '2026-09-23T16:41:00+00:00',
  account: { account_id_masked: '••••4118', cash: 12.5, equity: 101.84, buying_power: 12.5, type: 'individual' },
  holdings: [
    { symbol: 'VTI', quantity: 0.25, average_cost: 376, price: 381.4, price_at: '2026-09-23T16:40:58+00:00',
      market_value_usd: 95.35, unrealized_pct: 1.44 },
    { symbol: 'BND', quantity: 0.5, average_cost: 71, price: null, price_at: null, market_value_usd: null, unrealized_pct: null }],
  holdings_error: 'no usable quote for BND' });

test('the Agentic view leads with the venue’s live balance and holdings; the ledger keeps its own date', () => {
  const c = ledgerPage(async () => ({}), readableLedger);
  c.CT.microLive = liveVenue(); c.render();
  assert.equal(c.element('live-card').hidden, false);
  assert.equal(c.element('live-total').textContent, c.Console.usd(101.84));
  assert.match(c.element('live-stamp').textContent, /pf.liveStamp/);
  assert.match(c.element('live-stats').innerHTML, /pf.cash[\s\S]*\$12\.50[\s\S]*pf.buyingPower/);
  assert.equal(c.element('live-note').hidden, true);
  assert.equal(c.element('live-holdings').hidden, false);
  const rows = c.element('live-rows').innerHTML;
  assert.match(rows, /VTI[\s\S]*\$95\.35[\s\S]*BND/, 'largest first, as the service ordered them');
  assert.match(rows, /BND[\s\S]*pf-row-value">—/, 'a holding without a quote is a gap');
  assert.doesNotMatch(rows, /35\.50/, 'and never valued at its cost of 0.5 × $71');
  assert.match(c.element('live-foot').textContent, /no usable quote for BND/);
  assert.equal(c.element('bucket-cards').hidden, true, 'the same holdings are not listed twice');
  assert.equal(c.element('pf-total').textContent, c.Console.usd(100), 'the ledger keeps its own figure');
  assert.match(c.element('pf-asof').textContent, /pf.ledgerPriced/);
  assert.equal(c.element('strategy-heading').textContent, 'pf.ledgerTitle');
});

test('a venue that cannot be read shows a gap and leaves the ledger standing', () => {
  const c = ledgerPage(async () => ({}), readableLedger);
  c.CT.microLive = { connected: false, connect_error: 'no Robinhood OAuth token', checked_at: '2026-09-23T16:41:00+00:00',
                     account: null, holdings: null, holdings_error: null };
  c.render();
  assert.equal(c.element('live-total').textContent, '—', 'an unread balance is never drawn as $0');
  assert.match(c.element('live-note').textContent, /pf.liveOff.*no Robinhood OAuth token/);
  assert.equal(c.element('live-holdings').hidden, true);
  assert.equal(c.element('bucket-cards').hidden, false, 'the ledger’s own positions stand in');
  assert.equal(c.element('pf-total').textContent, c.Console.usd(100));

  const unlisted = ledgerPage(async () => ({}), readableLedger);
  unlisted.CT.microLive = { ...liveVenue(), holdings: null, holdings_error: 'BrokerUnavailable: positions' };
  unlisted.render();
  assert.equal(unlisted.element('live-total').textContent, unlisted.Console.usd(101.84));
  assert.match(unlisted.element('live-note').textContent, /pf.liveHoldingsOff/);
  assert.equal(unlisted.element('bucket-cards').hidden, false);
});

test('when the market data is behind the ledger the notice says so and does not offer confirming', () => {
  const behind = ledgerPage(async () => ({}), { ...heldBack,
    errors: { micro_live: 'portfolio state as_of 2026-09-23 is after the trade date 2026-09-21' },
    error_codes: { micro_live: 'ledger_ahead' } });
  behind.CT.microLive = liveVenue(); behind.render();
  assert.equal(behind.element('pf-total').textContent, '—');
  assert.equal(behind.element('ledger-held').textContent, 'pf.heldPricesBehind');
  assert.equal(behind.element('confirm-ledger').hidden, true, 'confirming would only stamp the same date');
  assert.match(behind.element('ledger-reason').textContent, /after the trade date/);
  assert.equal(behind.element('live-total').textContent, behind.Console.usd(101.84), 'the money is still shown');

  const unpriced = ledgerPage(async () => ({}), { ...heldBack, error_codes: { micro_live: 'prices_unavailable' } });
  assert.equal(unpriced.element('ledger-held').textContent, 'pf.heldPricesMissing');
  assert.equal(unpriced.element('confirm-ledger').hidden, true);

  const stale = ledgerPage(async () => ({}), { ...heldBack, error_codes: { micro_live: 'ledger_stale' } });
  assert.equal(stale.element('ledger-held').textContent, 'pf.ledgerHeld');
  assert.equal(stale.element('confirm-ledger').hidden, false, 'a ledger nobody confirmed is offered the check');
});

/* The daily job's refusal over cash carries the figure. The card shows it before the ledger
   has expired, and records it as what the reader says it was — on a second tap, with the
   amount filled in — then runs the same check again. */
const refusedOverCash = {
  date: '2026-09-17', confirmed: false, at: '2026-09-17T10:45:00+00:00',
  ledgers: [{ ledger: 'micro_live', bucket: 'micro_live', error: 'CashMismatch: cash mismatch: broker reports $350.00',
              cash_difference_usd: 250, venue_cash_usd: 350, ledger_cash_usd: 100 }],
};

test('a cash difference the check found is shown with the figures while the ledger is still current', async () => {
  const c = ledgerPage(async () => ({}), { ...heldBack, errors: {} }, async () => refusedOverCash);
  await flush();
  assert.equal(c.element('ledger-notice').hidden, false);
  assert.equal(c.element('ledger-held').hidden, true, 'the figures are not withheld yet');
  assert.match(c.element('ledger-check').textContent, /pf.lastCheck.*pf.checkRefused/);
  assert.equal(c.element('ledger-diff').hidden, false);
  assert.match(c.element('diff-text').textContent, /pf.cashMore.*250/);
  assert.match(c.element('book-transfer').textContent, /cf.deposit/);
  assert.match(c.element('book-income').textContent, /cf.income/);

  const agreed = ledgerPage(async () => ({}), { ...heldBack, errors: {} },
    async () => ({ date: '2026-09-17', confirmed: true, ledgers: [{ ledger: 'micro_live', confirmed: true }] }));
  await flush();
  assert.equal(agreed.element('ledger-notice').hidden, true);
});

test('recording the difference takes a second tap, sends the figure as the chosen kind and checks again', async () => {
  const sent = [];
  const post = async (path, body) => {
    sent.push([path, body]);
    return path === '/portfolio/confirm'
      ? { confirmed: true, ledgers: [{ ledger: 'micro_live', confirmed: true }] }
      : { bucket: 'micro_live', cash_usd: 350 };
  };
  const c = ledgerPage(post, { ...heldBack, errors: {} }, async () => refusedOverCash);
  await flush();

  await c.element('book-transfer').events.click();
  assert.equal(sent.length, 0, 'the first tap only arms the button');
  assert.match(c.element('book-transfer').textContent, /pf.tapAgain/);
  await c.element('book-income').events.click();
  assert.equal(sent.length, 0, 'switching buttons re-arms rather than sends');
  await c.element('book-income').events.click(); await flush();

  assert.deepEqual(sent.map(([path]) => path), ['/portfolio/cashflow', '/portfolio/confirm']);
  /* Objects made inside the page's realm have another prototype, so compare by value. */
  assert.equal(JSON.stringify(sent[0][1]), JSON.stringify({ bucket: 'micro_live', amount_usd: 250, kind: 'income',
                                                            reason: 'recorded from the ledger check of 2026-09-17' }));
  assert.equal(JSON.stringify(sent[1][1]), JSON.stringify({ ledger: 'micro_live' }));
  assert.match(c.element('book-status').textContent, /pf.booked/);
  assert.equal(c.reread, true, 'the figures are read again after the book changed');

  const less = ledgerPage(post, { ...heldBack, errors: {} }, async () => ({ ...refusedOverCash,
    ledgers: [{ ...refusedOverCash.ledgers[0], cash_difference_usd: -40 }] }));
  await flush();
  assert.match(less.element('diff-text').textContent, /pf.cashLess.*40/);
  assert.match(less.element('book-transfer').textContent, /cf.withdrawal/);
  assert.match(less.element('book-income').textContent, /cf.fee/);
  sent.length = 0;
  await less.element('book-transfer').events.click(); await less.element('book-transfer').events.click(); await flush();
  assert.deepEqual(sent[0][1].kind, 'withdrawal'); assert.equal(sent[0][1].amount_usd, 40);
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
  const health = c.CT_API.state.resources.health;
  health.lastOkAt = health.dataAt = Date.now() - 89000;
  assert.equal(c.CT_API.isLive('health'), true, 'three thirty-second polls');
  health.lastOkAt = health.dataAt = Date.now() - 91000;
  assert.equal(c.CT_API.isLive('health'), false);
});

const dated = (data, serverNow = Date.now()) => ({ ...response(data),
  headers: { get: name => name === 'date' ? new Date(serverNow).toUTCString() : null } });
const iso = ms => new Date(ms).toISOString().replace(/\.(\d{3})Z$/, '.$1456+00:00');

test('a reading the API kept is judged by when the API read it, not when the poll came back', async () => {
  /* /portfolio is kept by the API for up to eight minutes and renewed every four. A poll that
     came back a second ago can carry a reading nine minutes old, and the console has to say so
     rather than call it current. The stamp carries microseconds, as Python writes it. */
  const c = context();
  let checked = Date.now() - 9 * 60000;
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : dated({ buckets: {}, accounts: {}, errors: {}, combined: {}, checked_at: iso(checked) });
  c.run('api.js'); c.CT_API.start(['portfolio']); await flush();
  assert.equal(c.CT_API.isLive('portfolio'), true, 'nine minutes is within the ten allowed');

  checked = Date.now() - 11 * 60000;
  await c.CT_API.readOnce('portfolio');
  assert.equal(c.CT_API.isLive('portfolio'), false, 'eleven minutes is not, however fresh the poll');
  assert.equal(c.CT_API.canAct(['portfolio']), false);
});

test('the age of a stamped reading is taken on the API clock, not a phone that runs fast', async () => {
  const c = context();
  const serverNow = Date.now() - 5 * 60000;            // this device is five minutes ahead
  c.fetch = async url => url === '/auth/session' ? dated({ authenticated: true, csrf_token: 'csrf' }, serverNow)
    : dated({ status: 'READY', connected: true, checks: [], checked_at: iso(serverNow - 30000) }, serverNow);
  c.run('api.js'); c.CT_API.start(['microLive']); await flush();
  const item = c.CT_API.state.resources.microLive;
  assert.ok(Math.abs(Date.now() - item.dataAt - 30000) < 2000, 'thirty seconds old, not five and a half minutes');
  assert.equal(c.CT_API.isLive('microLive'), true);
});

test('one slow answer does not put the assistant out of date', async () => {
  /* The assistant polls every five seconds. Judged at two polls, a single read that took more
     than ten seconds -- one Robinhood handshake elsewhere in the API was enough -- showed the
     assistant as expired. */
  const c = context();
  c.fetch = async url => url === '/auth/session' ? response({ authenticated: true, csrf_token: 'csrf' })
    : response({ notes: {}, jobs: [], decisions: [], snapshot: {} });
  c.run('api.js'); c.CT_API.start(['advisor']); await flush();
  const advisor = c.CT_API.state.resources.advisor;
  advisor.lastOkAt = advisor.dataAt = Date.now() - 20000;
  assert.equal(c.CT_API.isLive('advisor'), true);
  advisor.lastOkAt = advisor.dataAt = Date.now() - 301000;
  assert.equal(c.CT_API.isLive('advisor'), false, 'five minutes without a reading is an outage');
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
    ticker: 'VTI', status: 'WAITING_FOR_APPROVAL', is_real_money: false, sell_quantity: null,
    authority_version: 'authority-a', authority_changed: null };
  c.CT.authority = { version: 'authority-a', preflight: { ready: true, stale: false },
    actions: { BUY: { human_approval: { permitted: true, blocked_by: [] } } } };
  c.CT.health.paused = false; c.CT.health.market.is_open = true;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { canAct: () => true, readOnce: async () => {}, selectProposal: () => {}, post: async () => ({ status: 'SUBMITTED', message: 'pending' }) };
  c.run('pages/approvals.js');
  return c;
}

function exitApprovalPage() {
  const c = approvalPage(), p = c.CT.proposalDetail;
  Object.assign(p, { action: 'SELL', sell_quantity: 0.2, exit: {
    action: 'SELL', authority_action: 'SELL', intent: 'close_position', authorised_quantity: 0.2,
    sellable: { quantity: 0.2, ledger_quantity: 0.2, venue_quantity: 0.2,
      venue_as_of: '2026-09-19T15:04:02Z', venue_stale: false, committed_quantity: 0,
      basis: 'both_sources_agree', agrees: true, note: null },
    expected_submit_quantity: 0.2, expected_is_estimate: false, expected_remaining: 0,
    expected_remaining_basis: 'both_sources_agree', expected_closes_position: true,
    min_sellable_increment: 0.000001, already_filled: 0,
    chain: { root_proposal_id: 'A', allowance: 0.2, filled: 0, committed: 0.2, remaining_allowance: 0,
      members: [{ proposal_id: 'A', status: 'PROPOSED', authorised_quantity: 0.2, filled_quantity: 0 }] },
    continuable: { eligible: false, quantity: 0, blocked_by: [{ code: 'not_attempted', detail: 'not submitted' }] },
    blocked_by: [],
  } });
  c.render();
  return c;
}

test('exit review uses shares, server closure state and distinct quantity bases', () => {
  const c = exitApprovalPage(), e = c.CT.proposalDetail.exit;
  assert.match(c.element('d-headline').textContent, /ap.exitShares 0.2/);
  assert.match(c.element('exit-detail').innerHTML, /ap.exitWillClose/);
  // SELL and close_position do not imply closure when the holding grows.
  e.expected_closes_position = false; e.expected_remaining = 0.1;
  e.expected_remaining_basis = 'larger_of_two';
  Object.assign(e.sellable, { venue_quantity: 0.3, agrees: false, basis: 'smaller_of_two', note: '<script>unsafe</script>' });
  c.render();
  const html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitWillRemain/); assert.doesNotMatch(html, /ap.exitWillClose/);
  assert.match(html, /ap.exitBasis.larger_of_two/); assert.match(html, /ap.exitBasis.smaller_of_two/);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.equal(c.element('btn-approve').disabled, false, 'disagreement alone does not refuse an exit');
});

test('unknown and stale exit quantities never render as zero or expected closure', () => {
  const c = exitApprovalPage(), e = c.CT.proposalDetail.exit;
  e.sellable.venue_quantity = null; e.sellable.venue_as_of = null; e.sellable.agrees = null;
  e.sellable.basis = 'ledger_only'; e.expected_is_estimate = true;
  e.expected_remaining = null; e.expected_closes_position = null; e.expected_remaining_basis = 'unknown';
  c.render();
  let html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitVenue<\/dt><dd>ap.exitUnread/);
  assert.match(html, /ap.exitRemaining<\/dt><dd>ap.exitCannotConfirm/);
  assert.match(html, /ap.exitEstimate/); assert.doesNotMatch(html, /ap.exitWillClose/);
  e.sellable.venue_quantity = 0.3; e.sellable.venue_stale = true; c.render();
  assert.match(c.element('exit-detail').innerHTML, /ap.exitStale/);
});

test('exit blocks and unavailable detail prevent both approval entry and a checked submission', async () => {
  const c = exitApprovalPage(); let posts = 0; c.CT_API.post = async () => { posts++; };
  c.element('btn-approve').events.click();
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  assert.equal(c.element('approve-submit').disabled, false);
  c.CT.proposalDetail.exit.blocked_by = [{ code: 'order_working', detail: 'still at venue' }]; c.render();
  assert.equal(c.element('btn-approve').hidden, true);
  assert.equal(c.element('approve-confirm').checked, false);
  await c.element('approve-submit').events.click(); assert.equal(posts, 0);
  c.CT.proposalDetail.exit = null; c.render();
  assert.match(c.element('exit-detail').innerHTML, /ap.exitUnavailable/);
  assert.equal(c.element('btn-approve').disabled, true);
});

test('exit readings invalidate confirmation without a proposal or authority version change', async () => {
  const c = exitApprovalPage(); let posts = 0; c.CT_API.post = async () => { posts++; };
  c.element('btn-approve').events.click();
  assert.match(c.element('approve-dialog-title').textContent, /ap.exitShares 0.2/);
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  c.CT.proposalDetail.exit.expected_submit_quantity = 0.1;
  // The handler rechecks even before the render callback has run.
  await c.element('approve-submit').events.click(); assert.equal(posts, 0);
  c.render(); assert.equal(c.element('approve-confirm').checked, false);
  assert.equal(c.element('approve-submit').disabled, true);
});

test('live REDUCE reads its own authority and rejects inconsistent permission or terms', () => {
  const c = exitApprovalPage(), p = c.CT.proposalDetail;
  p.is_real_money = true; p.action = p.exit.authority_action = p.exit.action = 'REDUCE';
  c.CT.health.micro_live_enabled = true; c.CT.microLive.preflight_ready = true;
  c.CT.authority.actions.REDUCE = { human_approval: { permitted: true, blocked_by: [] } };
  c.CT.authority.actions.SELL = { human_approval: { permitted: false, blocked_by: [{ detail: 'sell blocked' }] } };
  c.render(); assert.equal(c.element('btn-approve').disabled, false);
  c.CT.authority.actions.REDUCE.human_approval.blocked_by = [{ detail: 'reduce blocked' }];
  c.render(); assert.equal(c.element('btn-approve').disabled, true);
  assert.match(c.element('exit-detail').innerHTML, /reduce blocked/);
  assert.doesNotMatch(c.element('exit-detail').innerHTML, /sell blocked/);
  c.CT.authority.actions.REDUCE.human_approval.blocked_by = [];
  p.exit.authorised_quantity = 0.3; c.render(); assert.equal(c.element('btn-approve').disabled, true);
});

test('exit chain shows fills and unfilled commitments separately, without writing on render', () => {
  const c = exitApprovalPage(), p = c.CT.proposalDetail, e = p.exit;
  p.status = 'CANCELED'; e.already_filled = 0.12;
  Object.assign(e.chain, { filled: 0.12, committed: 0, remaining_allowance: 0.08 });
  e.chain.members = [{ proposal_id: 'root<&', status: 'CANCELED', authorised_quantity: 0.2, filled_quantity: 0.12 }];
  e.continuable = { eligible: true, quantity: 0.08, blocked_by: [] }; c.render();
  const html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitChainFilled<\/dt><dd>ap.exitShares 0.12/);
  assert.match(html, /ap.exitChainRemaining<\/dt><dd>ap.exitShares 0.08/);
  assert.match(html, /ap.exitContinuable ap.exitShares 0.08/);
  assert.match(html, /proposal=root%3C%26/);
  assert.doesNotMatch(html, /<button|continue_exit/);
  assert.equal(c.element('btn-approve').hidden, true);
  p.action = 'BUY'; p.exit = null; c.render();
  assert.equal(c.element('exit-detail').hidden, true);
  assert.equal(c.element('exit-detail').innerHTML, '');
});

function continuationPage() {
  const c = exitApprovalPage(), p = c.CT.proposalDetail;
  p.status = 'CANCELED'; p.exit.already_filled = 0.12;
  Object.assign(p.exit.chain, { filled: 0.12, committed: 0, remaining_allowance: 0.08 });
  p.exit.continuable = { eligible: true, quantity: 0.08, blocked_by: [] };
  c.render(); return c;
}
function confirmContinuation(c) {
  c.element('btn-continue-exit').events.click();
  c.element('continue-confirm').checked = true; c.element('continue-confirm').events.change();
}
const continuationAnswer = () => ({ deduplicated: false, continuation_of: 'A', root_proposal_id: 'A',
  authorised_quantity: 0.08, research_anchor_at: '2026-09-19T13:00:00Z', continued_at: '2026-09-19T14:00:00Z',
  proposal: { proposal_id: 'B', status: 'WAITING_FOR_APPROVAL' } });

test('continuation double click creates once with request_id and opens a fresh detail without approving', async () => {
  const c = continuationPage(), posts = [], reads = [], selected = []; let resolve;
  c.CT_API.post = (url, body) => { posts.push({ url, body }); return new Promise(r => { resolve = r; }); };
  c.CT_API.readOnce = async key => { reads.push(key); };
  c.CT_API.selectProposal = (id, options) => { selected.push({ id, options }); c.CT.proposalDetail = null; };
  assert.equal(c.element('btn-continue-exit').hidden, false);
  confirmContinuation(c); assert.equal(c.element('continue-submit').disabled, false);
  const first = c.element('continue-submit').events.click();
  await c.element('continue-submit').events.click(); assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/proposals/A/continue_exit');
  assert.equal(posts[0].body.confirm, true);
  assert.equal(posts[0].body.authority_version, 'authority-a');
  assert.match(posts[0].body.request_id, /^[a-f0-9-]{36}$/);
  assert.equal(posts[0].body.idempotency_key, undefined);
  resolve(continuationAnswer()); await first;
  assert.equal(posts.length, 1); assert.equal(selected[0].id, 'B');
  assert.equal(selected[0].options.includeClosed, true);
  assert.ok(reads.includes('proposalDetail')); assert.ok(reads.includes('attention'));
  assert.match(c.element('result-slot').innerHTML, /ap.continueCreated/);
  assert.match(c.element('result-slot').innerHTML, /proposal=B/);
  assert.equal(c.element('approve-dialog').open, undefined);
});

test('continuation 409 refreshes without replay; changed authority needs a separate acknowledgement', async () => {
  const c = continuationPage(), posts = [];
  c.CT_API.post = async (url, body) => {
    posts.push(body);
    if (posts.length === 1) throw Object.assign(Error('authority moved'), { status: 409 });
    return continuationAnswer();
  };
  c.CT_API.readOnce = async key => {
    if (key === 'authority') {
      c.CT.authority.version = 'authority-b';
      c.CT.proposalDetail.authority_changed = { from: 'authority-a', to: 'authority-b', classification: 'loosened' };
    }
  };
  confirmContinuation(c); await c.element('continue-submit').events.click();
  assert.equal(posts.length, 1); assert.match(c.element('result-slot').innerHTML, /ap.continueConflict/);
  confirmContinuation(c);
  assert.equal(c.element('continue-authority-ack-row').hidden, false);
  assert.equal(c.element('continue-submit').disabled, true);
  await c.element('continue-submit').events.click(); assert.equal(posts.length, 1);
  c.element('continue-authority-confirm').checked = true; c.element('continue-authority-confirm').events.change();
  await c.element('continue-submit').events.click(); assert.equal(posts.length, 2);
  assert.equal(posts[1].authority_version, 'authority-b'); assert.equal(posts[1].acknowledge_authority_change, true);
});

test('lost continuation response keeps the same id only for an explicit retry and accepts deduplication', async () => {
  const c = continuationPage(), posts = [];
  c.CT_API.post = async (url, body) => {
    posts.push(body);
    if (posts.length === 1) throw Error('response lost');
    return { ...continuationAnswer(), deduplicated: true };
  };
  confirmContinuation(c); await c.element('continue-submit').events.click();
  assert.equal(posts.length, 1); assert.match(c.element('result-slot').innerHTML, /ap.continueRetry/);
  assert.equal(c.element('continue-confirm').checked, false);
  confirmContinuation(c); await c.element('continue-submit').events.click();
  assert.equal(posts.length, 2); assert.equal(posts[0].request_id, posts[1].request_id);
  assert.match(c.element('result-slot').innerHTML, /ap.continueExisting/);
});

test('continuation confirmation cannot survive quantity, authority, proposal or page changes', async () => {
  for (const change of [
    c => { c.CT.proposalDetail.exit.continuable.quantity = 0.04; },
    c => { c.CT.authority.version = 'authority-b'; },
    c => { c.CT.proposalDetail.proposal_id = 'other-account-proposal'; },
    c => { c.document.dispatchEvent({ type: 'ct:leave' }); },
    c => { c.CT_API.canAct = () => false; },
  ]) {
    const c = continuationPage(); let posts = 0; c.CT_API.post = async () => { posts++; };
    confirmContinuation(c); change(c);
    await c.element('continue-submit').events.click(); assert.equal(posts, 0);
    c.render(); assert.equal(c.element('continue-confirm').checked, false);
  }
});

test('late continuation creation does not select a proposal after leaving the page', async () => {
  const c = continuationPage(); let resolve, selections = 0;
  c.CT_API.post = () => new Promise(r => { resolve = r; });
  c.CT_API.selectProposal = () => { selections++; };
  confirmContinuation(c); const pending = c.element('continue-submit').events.click();
  c.document.dispatchEvent({ type: 'ct:leave' }); resolve(continuationAnswer()); await pending;
  assert.equal(selections, 0);
});

test('late continuation creation does not replace another proposal reached by a same-page link', async () => {
  const c = continuationPage(), selected = []; let resolve;
  c.CT_API.post = () => new Promise(r => { resolve = r; });
  c.CT_API.selectProposal = id => { selected.push(id); };
  confirmContinuation(c); const pending = c.element('continue-submit').events.click();
  c.location.search = '?proposal=another-root';
  c.location.href = 'https://console.test/ui/approvals.html?proposal=another-root';
  c.document.dispatchEvent({ type: 'ct:route' }); resolve(continuationAnswer()); await pending;
  assert.deepEqual(selected, ['another-root']);
});

test('continuation_waiting, unknown orders and expired research hide creation; market closure does not stop proposal creation', () => {
  const c = continuationPage(), onward = c.CT.proposalDetail.exit.continuable;
  c.CT.health.market.is_open = false; c.render(); assert.equal(c.element('btn-continue-exit').hidden, false);
  for (const code of ['continuation_waiting', 'reconciliation_unresolved', 'order_working', 'research_expired']) {
    onward.eligible = false; onward.blocked_by = [{ code, detail: 'server refusal' }]; c.render();
    assert.equal(c.element('btn-continue-exit').hidden, true);
    assert.match(c.element('exit-detail').innerHTML, new RegExp('ap.exitBlock.' + code));
  }
});

test('exit result trusts post-submission three-state confirmation, not an EXECUTED status or sell intent', async () => {
  for (const closed of [null, false, true]) {
    const c = exitApprovalPage();
    c.CT_API.post = async () => ({ status: 'EXECUTED', message: 'filled', exit_result: {
      requested_quantity: 0.2, submitted_quantity: 0.2, filled_quantity: 0.2,
      confirmed_remaining: closed == null ? null : closed ? 0 : 0.1, position_closed: closed,
      confirmed_remaining_basis: closed == null ? 'unknown' : 'both_sources_agree',
      chain: { filled: 0.2, committed: 0, remaining_allowance: 0 },
      continuable: { eligible: false, quantity: 0, blocked_by: [{ code: 'allowance_spent', detail: 'all used' }] },
    } });
    c.element('btn-approve').events.click(); c.element('approve-confirm').checked = true;
    c.element('approve-confirm').events.change(); await c.element('approve-submit').events.click();
    const html = c.element('result-slot').innerHTML;
    assert.match(html, /ap.exitResultTitle/);
    if (closed === true) assert.match(html, /ap.exitClosed/);
    else assert.doesNotMatch(html, /ap.exitClosed/);
    if (closed == null) assert.match(html, /ap.exitConfirmedRemaining<\/dt><dd>ap.exitCannotConfirm/);
    if (closed === false) assert.match(html, /ap.exitStillHeld/);
  }
});

test('autonomous floor refusal is visible without disabling an otherwise permitted human reduction', () => {
  const c = exitApprovalPage(), p = c.CT.proposalDetail;
  p.action = p.exit.action = p.exit.authority_action = 'REDUCE'; p.is_real_money = true;
  c.CT.health.micro_live_enabled = true; c.CT.microLive.preflight_ready = true;
  c.CT.authority.actions.REDUCE = {
    human_approval: { permitted: true, blocked_by: [] },
    autonomous: { permitted: false, blocked_by: [{ code: 'no_min_remaining', detail: 'account floor not declared' }] },
  };
  p.exit.autonomous = { permitted: false, min_remaining_pct_of_opened: null,
    position_high_water_quantity: 100, high_water_set_at: '2026-09-12T14:02:11Z', high_water_source: 'console',
    cycle_started_at: '2026-09-12T14:02:11Z', current_quantity: 26, floor_quantity: null, headroom_quantity: null,
    blocked_by: [{ code: 'no_min_remaining', detail: 'not declared <script>unsafe</script>' }] };
  c.render(); const html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitAutoPositionBlocked/);
  assert.match(html, /ap.exitAutoPct<\/dt><dd>ap.exitAutoUndeclared/);
  assert.match(html, /ap.exitAutoFloor<\/dt><dd>ap.exitUnread/);
  assert.match(html, /ap.exitAutoHeadroom<\/dt><dd>ap.exitUnread/);
  assert.match(html, /ap.exitAutoBlock.no_min_remaining/);
  assert.match(html, /account floor not declared/);
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.equal(c.element('btn-approve').disabled, false);
  c.element('btn-approve').events.click(); c.element('approve-confirm').checked = true;
  c.element('approve-confirm').events.change(); assert.equal(c.element('approve-submit').disabled, false);
});

test('autonomous headroom shows zero and one exactly, never recomputes a baseline or leaks into SELL', () => {
  const c = exitApprovalPage(), p = c.CT.proposalDetail;
  p.action = p.exit.action = p.exit.authority_action = 'REDUCE';
  c.render(); assert.match(c.element('exit-detail').innerHTML, /ap.exitAutoUnknown/);
  p.exit.autonomous = { permitted: false, min_remaining_pct_of_opened: 25,
    position_high_water_quantity: 100, current_quantity: 25, floor_quantity: 25, headroom_quantity: 0,
    high_water_source: 'cli', blocked_by: [{ code: 'below_floor', detail: 'at floor' }] };
  c.render(); assert.match(c.element('exit-detail').innerHTML, /ap.exitAutoHeadroom<\/dt><dd>ap.exitShares 0/);
  Object.assign(p.exit.autonomous, { permitted: true, current_quantity: 26, headroom_quantity: 1, blocked_by: [] });
  c.render(); let html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitAutoHighWater<\/dt><dd>ap.exitShares 100/);
  assert.match(html, /ap.exitAutoFloor<\/dt><dd>ap.exitShares 25/);
  assert.match(html, /ap.exitAutoHeadroom<\/dt><dd>ap.exitShares 1/);
  assert.match(html, /ap.exitAutoPositionReady/); assert.match(html, /ap.exitAutoBoundary/);
  // The server owns the numbers; missing provenance must stay unavailable.
  p.exit.autonomous.position_high_water_quantity = null; p.exit.autonomous.headroom_quantity = null;
  p.exit.autonomous.blocked_by = [{ code: 'no_opened_baseline', detail: 'ledger cannot be read' }];
  c.render(); html = c.element('exit-detail').innerHTML;
  assert.match(html, /ap.exitAutoHighWater<\/dt><dd>ap.exitUnread/);
  assert.match(html, /ledger cannot be read/);
  p.action = p.exit.action = p.exit.authority_action = 'SELL'; p.exit.autonomous = null;
  c.render(); assert.doesNotMatch(c.element('exit-detail').innerHTML, /exit-autonomous|ap.exitAutoTitle/);
});

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
  assert.equal(sent.body.authority_version, 'authority-a');
  assert.equal(sent.body.acknowledge_authority_change, false);
  assert.match(sent.body.idempotency_key, /^[0-9a-f-]{36}$/);
  resolve({ status: 'SUBMITTED', message: 'pending' }); await first;
  assert.ok(c.element('result-slot').innerHTML.includes('Submitted'));
  assert.ok(!c.element('result-slot').innerHTML.includes('card-real'));
});

test('authority changes invalidate a checked approval even if the proposal terms stay identical', async () => {
  const c = approvalPage(); let posted = 0;
  c.CT_API.post = async () => { posted++; };
  c.element('btn-approve').events.click();
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  c.CT.authority.version = 'authority-b'; c.render();
  assert.equal(c.element('approve-confirm').checked, false);
  assert.equal(c.element('approve-submit').disabled, true);
  assert.equal(c.element('btn-approve').disabled, true, 'the proposal detail still describes the previous authority');
  c.CT.authority.version = 'authority-a'; c.render();
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  await c.element('approve-submit').events.click();
  assert.equal(posted, 0, 'returning to the original version does not restore a discarded confirmation');
});

test('loosened and unknown authority require a separate acknowledgement bound to the shown version', async () => {
  for (const classification of ['loosened', 'mixed', 'unknown']) {
    const c = approvalPage(); let sent;
    c.CT_API.post = async (url, body) => { sent = body; return { status: 'SUBMITTED' }; };
    c.CT.proposalDetail.authority_version = classification === 'unknown' ? null : 'research-version';
    c.CT.proposalDetail.authority_changed = { from: c.CT.proposalDetail.authority_version, to: 'authority-a',
      classification, paths: ['buckets.micro_live.autonomy.open.enabled'], detail: '<script>untrusted</script>' };
    c.render(); c.element('btn-approve').events.click();
    assert.equal(c.element('approve-authority-ack-row').hidden, false);
    assert.match(c.element('approve-authority-summary').textContent, /buckets.micro_live.autonomy.open.enabled/);
    assert.equal(c.element('approve-authority-summary').innerHTML, '', 'API text is assigned as text, never HTML');
    c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
    assert.equal(c.element('approve-submit').disabled, true);
    await c.element('approve-submit').events.click(); assert.equal(sent, undefined);
    c.element('approve-authority-confirm').checked = true; c.element('approve-authority-confirm').events.change();
    assert.equal(c.element('approve-submit').disabled, false);
    await c.element('approve-submit').events.click();
    assert.equal(sent.authority_version, 'authority-a');
    assert.equal(sent.acknowledge_authority_change, true);
  }
});

test('tightened authority shows the change without asking for a loosening acknowledgement', () => {
  const c = approvalPage();
  c.CT.proposalDetail.authority_version = 'research-version';
  c.CT.proposalDetail.authority_changed = { from: 'research-version', to: 'authority-a', classification: 'tightened', paths: [] };
  c.render(); c.element('btn-approve').events.click();
  assert.equal(c.element('approve-authority-change').hidden, false);
  assert.equal(c.element('approve-authority-ack-row').hidden, true);
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  assert.equal(c.element('approve-submit').disabled, false);
});

test('missing, stale, or blocked authority cannot enable live approval', () => {
  const c = approvalPage();
  c.CT.proposalDetail.is_real_money = true;
  c.CT.health.micro_live_enabled = true; c.CT.microLive.preflight_ready = true;
  c.render(); assert.equal(c.element('btn-approve').disabled, false);
  c.CT.authority.actions.BUY.human_approval.permitted = false;
  c.render(); assert.equal(c.element('btn-approve').disabled, true);
  c.CT.authority.actions.BUY.human_approval.permitted = true;
  c.CT.authority.preflight.stale = true;
  c.render(); assert.equal(c.element('btn-approve').disabled, true, 'a fresh response cannot renew a stale preflight');
  c.CT.authority.preflight.stale = false;
  c.CT_API.canAct = keys => !keys.includes('authority');
  c.render(); assert.equal(c.element('btn-approve').disabled, true);
  c.CT_API.canAct = () => true; c.CT.authority = null;
  c.render(); assert.equal(c.element('btn-approve').disabled, true);
});

test('a 409 refreshes authority and proposal without replaying, then requires a new explicit review', async () => {
  const c = approvalPage(); const reads = [], sent = [];
  c.CT_API.post = async (url, body) => {
    sent.push(body);
    if (sent.length === 1) throw Object.assign(new Error('authority changed; review'), { status: 409 });
    return { status: 'SUBMITTED' };
  };
  c.CT_API.readOnce = async key => {
    reads.push(key);
    if (key === 'authority') c.CT.authority.version = 'authority-b';
    if (key === 'proposalDetail') c.CT.proposalDetail.authority_changed = {
      from: 'authority-a', to: 'authority-b', classification: 'loosened', paths: ['limit'] };
  };
  c.element('btn-approve').events.click(); c.element('approve-confirm').checked = true;
  await c.element('approve-submit').events.click();
  assert.deepEqual(reads, ['authority', 'proposals', 'proposalDetail']);
  assert.equal(sent.length, 1);
  assert.match(c.element('result-slot').innerHTML, /ap.reviewAgain/);
  assert.equal(c.element('btn-approve').disabled, false);
  c.element('btn-approve').events.click();
  assert.equal(c.element('approve-authority-confirm').checked, false);
  c.element('approve-confirm').checked = true; c.element('approve-authority-confirm').checked = true;
  await c.element('approve-submit').events.click();
  assert.equal(sent.length, 2);
  assert.equal(sent[1].authority_version, 'authority-b');
  assert.equal(sent[1].acknowledge_authority_change, true);
  assert.notEqual(sent[0].idempotency_key, sent[1].idempotency_key);
});

test('leaving the approval page discards both confirmation checkboxes', async () => {
  const c = approvalPage(); let posted = 0; c.CT_API.post = async () => { posted++; };
  c.element('btn-approve').events.click(); c.element('approve-confirm').checked = true;
  c.element('approve-authority-confirm').checked = true;
  c.document.dispatchEvent({ type: 'ct:leave' });
  assert.equal(c.element('approve-confirm').checked, false);
  assert.equal(c.element('approve-authority-confirm').checked, false);
  await c.element('approve-submit').events.click(); assert.equal(posted, 0);
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
  c.CT.health.warnings = [];
  if (portfolio) c.CT.portfolio = portfolio;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { canAct: () => true, readOnce: async () => {}, isLive: () => true };
  c.run('pages/index.js');
  return c;
}

function evaluationPage(report, scope = report.scope) {
  const c = context(); c.run('console.js'); c.location.search = '?scope=' + scope;
  c.CT.evaluation = report;
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { state: { resources: { evaluation: {} } }, selectEvaluation: () => {}, isLive: () => true };
  c.run('pages/evaluation.js'); return c;
}

function systemSchedulePage(schedule) {
  const c = context(true); c.run('console.js');
  c.CT.schedule = schedule;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { post: async () => ({}), readOnce: async () => {}, isLive: () => true };
  c.run('pages/system.js');
  return c;
}

test('System uses explicit activity states, excluding first-run waits despite legacy overdue=true', () => {
  const c = systemSchedulePage(null);
  c.CT.operations = { activities: [{ id: 'ledger_confirm', state: 'awaiting_first_run', overdue: true,
    expected_since: '2026-09-18T20:27:56Z', due_by: '2026-09-19T20:27:56Z', last_success_at: null }] };
  c.render();
  assert.equal(c.element('op-state').textContent, 'sy.opsWaiting 1');
  assert.doesNotMatch(c.element('op-state').className, /chip-warn/);
  const html = c.element('op-list').innerHTML;
  assert.match(html, /sy.opState.awaiting_first_run/); assert.match(html, /sy.opDue/);
  assert.doesNotMatch(html, /sy.never|sy.overdue|sy.opState.overdue|sy.opSuccess/);
});

test('System shows a fresh failure in full even with overdue=false and recovers on the next success', () => {
  const c = systemSchedulePage(null);
  const reason = 'CashMismatch: ' + 'diagnostic context '.repeat(35) + '\n101.50 versus 100.00; difference 1.50 <script>untrusted</script>';
  const row = { id: 'ledger_confirm', state: 'failed', overdue: false, due_by: '2026-09-20T10:45:02Z',
    last_attempt_at: '2026-09-19T10:45:12Z', last_failure: { at: '2026-09-19T10:45:12Z', reason } };
  c.CT.operations = { activities: [row] }; c.render();
  assert.equal(c.element('op-state').textContent, 'sy.opsNeedsAttention 1');
  assert.match(c.element('op-list').innerHTML, /sy.opState.failed/);
  assert.ok(c.element('op-list').innerHTML.includes(c.Console.escapeHtml(reason)));
  assert.doesNotMatch(c.element('op-list').innerHTML, /<script>/);
  // A retained historical report must not turn a recovered state back into a failure.
  row.state = 'current'; row.last_success_at = '2026-09-19T11:00:00Z'; c.render();
  assert.equal(c.element('op-state').textContent, 'sy.opsOk');
  assert.doesNotMatch(c.element('op-list').innerHTML, /CashMismatch|sy.opFailure/);
  row.state = 'awaiting_first_run'; row.last_failure = null; row.last_success_at = null; c.render();
  row.state = 'failed'; row.last_failure = { reason: 'First attempt failed' }; c.render();
  assert.match(c.element('op-list').innerHTML, /First attempt failed/);
});

test('System preserves backend state and unknown deadlines, while retaining a stopped attempt failure reason', () => {
  const c = systemSchedulePage(null);
  c.CT.operations = { activities: [
    { id: 'reconciliation', state: 'current', overdue: true, due_by: '2000-01-01T00:00:00Z' },
    { id: 'maintenance', state: 'awaiting_first_run', overdue: true },
    { id: 'ledger_confirm', state: 'overdue', overdue: false, due_by: null,
      last_failure: { reason: 'BrokerUnavailable: no venue session' } },
  ] }; c.render();
  assert.equal(c.element('op-state').textContent, 'sy.opsNeedsAttention 1 · sy.opsWaiting 1');
  assert.match(c.element('op-list').innerHTML, /sy.opDue<\/dt><dd>sy.unknown/);
  assert.match(c.element('op-list').innerHTML, /BrokerUnavailable: no venue session/);
  assert.match(c.element('op-list').innerHTML, /sy.opState.current/);
});

test('System never derives health from a missing state, empty activity list or a stale read', () => {
  const c = systemSchedulePage(null);
  c.CT.operations = { activities: [{ id: 'backup', overdue: false, last_success_at: '2026-09-19T10:00:00Z' }] }; c.render();
  assert.equal(c.element('op-state').textContent, 'sy.opsUnknown 1');
  assert.match(c.element('op-list').innerHTML, /sy.opState.unknown/);
  c.CT.operations.activities = []; c.render(); assert.equal(c.element('op-state').textContent, 'sy.unknown');
  c.CT.operations.activities = [{ id: 'backup', state: 'current', overdue: false }];
  c.CT_DEMO = false; c.CT_API.state = { resources: {} }; c.CT_API.isLive = () => false; c.render();
  assert.equal(c.element('op-state').textContent, 'sy.unknown');
  assert.match(c.element('op-list').innerHTML, /sy.opsStale/);
});

test('System shows each account schedule, its trigger cadence, and the protected exit capacity', () => {
  const c = context(true), schedule = JSON.parse(JSON.stringify(c.CT.schedule));
  schedule.tasks[0].reason = 'Account reason <script>';
  const page = systemSchedulePage(schedule);
  assert.equal(page.element('sc-mode').textContent, 'sy.perAccount');
  assert.equal(page.element('sc-fires').textContent, 'Daily');
  assert.match(page.element('sc-reserve').textContent, /1 6/);
  assert.equal(page.element('sc-task-section').hidden, false);
  const html = page.element('sc-tasks').innerHTML;
  assert.match(html, /bucket.micro_live · Holdings/);
  assert.match(html, /Candidates/);
  assert.match(html, /VTI, BND/);
  assert.match(html, /Account reason &lt;script&gt;/);
  assert.ok(!html.includes('<script>'));
});

test('System keeps the legacy schedule summary and clears account tasks when they are absent', () => {
  const c = context(true), legacy = JSON.parse(JSON.stringify(c.CT.schedule));
  delete legacy.tasks; delete legacy.per_account; delete legacy.fires_every; delete legacy.reserve_for_exits;
  const page = systemSchedulePage(legacy);
  assert.equal(page.element('sc-mode').textContent, 'sy.legacySchedule');
  assert.equal(page.element('sc-task-section').hidden, true);
  assert.equal(page.element('sc-period').textContent, 'daily · XNYS · America/New_York');
  page.CT.schedule = null; page.render();
  assert.equal(page.element('sc-task-section').hidden, true);
  assert.equal(page.element('sc-tasks').innerHTML, '');
});

test('evaluation keeps NOT_STARTED counts but hides performance and unstarted integrity claims', () => {
  const c = evaluationPage({ scope: 'agentic', state: 'NOT_STARTED', days_recorded: 0, declared: null,
    decisions: { research_runs: 17 }, execution: { fills: 3, traded_usd: 500 } });
  assert.equal(c.element('ev-state').textContent, 'evl.notStarted');
  assert.equal(c.element('ev-performance').hidden, true);
  assert.equal(c.element('ev-integrity-section').hidden, true);
  assert.equal(c.element('ev-not-started').hidden, false);
  assert.match(c.element('ev-decisions').innerHTML, /17/);
  assert.match(c.element('ev-decisions').innerHTML, /evl.fills 3/);
  assert.equal(c.element('evaluation-empty').hidden, true);
});

test('evaluation uses session gaps and stale state without treating a long incomplete sample as conclusive', () => {
  const c = evaluationPage({ scope: 'demo', state: 'RUNNING', days_recorded: 200, conclusive: true,
    sample: { basis: 'sessions', last_snapshot_on: '2026-09-10', stale: true, sessions_behind: 6,
      gaps: [{ from: '2026-09-01', to: '2026-09-05', missing_sessions: 2, reason: '<missing>' }] } });
  assert.equal(c.element('ev-sample-status').textContent, 'evl.sampleStale');
  assert.match(c.element('ev-sample-detail').textContent, /2026-09-10 6/);
  assert.match(c.element('ev-gaps').innerHTML, /evl.missingSessions 2/);
  assert.match(c.element('ev-gaps').innerHTML, /&lt;missing&gt;/);
  assert.equal(c.element('ev-interpretation').textContent, 'evl.incompleteSample');
  c.CT.evaluation.sample = { basis: 'unknown: calendar unavailable', stale: null, gaps: [] }; c.render();
  assert.equal(c.element('ev-sample-status').textContent, 'evl.sampleUnknown');
});

test('route quality distinguishes measured zero from unmeasured values and never displays returns', () => {
  const c = evaluationPage({ scope: 'route_quality', routes: [{ route: 'assistant<script>', runs: 2, complete: 2,
    failed: 0, timed_out: 0, limit_hits: 0, llm_calls_total: 0, fact_accuracy_pct: null, human_edit_rate_pct: null,
    format_pass_rate_pct: 80, evidence_check_pass_rate_pct: 0,
    evaluated: { failures: true, calls: false, evidence: true, format: false, facts: false, edits: false } }] });
  assert.equal(c.element('evaluation-report').hidden, true);
  assert.equal(c.element('evaluation-routes').hidden, false);
  const html = c.element('ev-routes').innerHTML;
  assert.match(html, /assistant&lt;script&gt;/);
  assert.match(html, /0\.00%/);
  assert.ok((html.match(/evl.notEvaluated/g) || []).length >= 5);
  assert.doesNotMatch(html, /80\.00%/);
  c.CT.evaluation.routes = []; c.render(); assert.match(c.element('ev-routes').innerHTML, /evl.noRoutes/);
});

test('evaluation hides mismatched reports, labels cached reports, and keeps scope navigation during failures', () => {
  const c = evaluationPage({ scope: 'demo', state: 'RUNNING' }, 'agentic');
  assert.equal(c.element('evaluation-report').hidden, true);
  assert.match(c.element('evaluation-scopes').innerHTML, /scope=route_quality/);
  c.CT_API.state.resources.evaluation.error = 'offline'; c.render();
  assert.equal(c.element('evaluation-error').textContent, 'offline');
  c.CT.evaluation = { scope: 'agentic', state: 'RUNNING' }; c.CT_API.isLive = () => false; c.render();
  assert.equal(c.element('evaluation-status').hidden, false);
  assert.match(c.element('evaluation-status').textContent, /evl.cached.*offline/);
  c.location.search = '?scope=route_quality'; c.document.dispatchEvent(new c.CustomEvent('ct:route', {}));
  assert.equal(c.element('evaluation-report').hidden, true);
  assert.equal(c.element('ev-scope-note').textContent, 'evl.scopeNote.route_quality');
});

test('scope switching clears the previous report and discards late success and failure responses', async () => {
  for (const fail of [false, true]) {
    const c = context(), pending = new Map();
    c.fetch = url => new Promise(resolve => pending.set(url, resolve));
    c.run('api.js'); c.CT.evaluation = { scope: 'agentic', strategy: { equity_usd: 123 } };
    c.CT_API.selectEvaluation('demo');
    assert.equal(c.CT.evaluation, null);
    c.CT_API.selectEvaluation('route_quality');
    pending.get('/evaluation?scope=demo')(fail ? response({ detail: 'old failure' }, 500) : response({ scope: 'demo' }));
    await flush(); assert.equal(c.CT.evaluation, null);
    assert.notEqual(c.CT_API.state.resources.evaluation.error, 'old failure');
    pending.get('/evaluation?scope=route_quality')(response({ scope: 'route_quality', routes: [] }));
    await flush(); assert.equal(c.CT.evaluation.scope, 'route_quality');
    assert.equal(c.CT_API.selectEvaluation('personal'), false);
  }
});

test('evaluation rejects an incorrectly scoped server payload and demo uses distinct fixtures without requests', async () => {
  const c = context(); c.fetch = async () => response({ scope: 'demo' }); c.run('api.js');
  await c.CT_API.readOnce('evaluation');
  assert.equal(c.CT.evaluation, null); assert.equal(c.CT_API.isLive('evaluation'), false);
  const demo = context(true); demo.run('api.js');
  for (const scope of ['agentic', 'route_quality', 'demo']) {
    demo.CT_API.selectEvaluation(scope); assert.equal(demo.CT.evaluation.scope, scope);
  }
});

function attentionPayload(items = [], readable = true) {
  return { checked_at: new Date().toISOString(), readable, unreadable: readable ? [] : ['ledger: unavailable'],
    items, top: items[0] || null, summary: { total: items.length, requires_person: items.filter(x => x.requires_person).length }, context: {} };
}
function attentionItem(overrides = {}) {
  return { id: 'waiting:A', kind: 'proposal_waiting', severity: 'high', priority: 3, requires_person: true,
    title: 'Review VTI', detail: 'Current proposal', scope: 'agentic', account_label: 'Agentic', subject: 'A',
    since: '2026-09-18T20:00:00Z', link: { page: 'approvals', params: { proposal: 'A' } }, ...overrides };
}

test('home hides the entire attention section only when the collector is readable, current and empty', () => {
  const c = homePage(); c.CT_DEMO = false;
  c.CT_API.watchRun = () => {};
  c.CT.attention = attentionPayload(); c.render();
  assert.equal(c.element('home-attention').hidden, true);
  assert.equal(c.element('needs-you-slot').innerHTML, '');
  c.CT.attention = attentionPayload([], false); c.render();
  assert.match(c.element('needs-you-slot').innerHTML, /at.unknown/);
  assert.equal(c.element('home-attention').hidden, false);
  assert.doesNotMatch(c.element('needs-you-slot').innerHTML, /at.clear/);
  c.CT.attention = attentionPayload(); c.CT_API.isLive = () => false; c.render();
  assert.match(c.element('needs-you-slot').innerHTML, /at.unknown/);
  assert.equal(c.element('home-attention').hidden, false);
  c.CT.attention = null; c.render();
  assert.equal(c.element('home-attention').hidden, false);
  assert.doesNotMatch(c.element('needs-you-slot').innerHTML, /at.clear/);
});

test('home puts system notices after actionable items in the same disclosure', () => {
  const c = homePage();
  c.CT.attention = attentionPayload([attentionItem()]);
  c.CT.health.warnings = ['Optional data <source> unavailable'];
  c.render();
  const html = c.element('needs-you-slot').innerHTML;
  assert.equal(c.element('home-attention').hidden, false);
  assert.ok(html.indexOf('data-attention-id="waiting:A"') < html.indexOf('data-od-id="warning-0"'));
  assert.match(html, /at.more 1/);
  assert.match(html, /Optional data &lt;source&gt; unavailable/);
  assert.doesNotMatch(html, /home-notices/);
  c.CT.attention = attentionPayload(); c.render();
  assert.equal(c.element('home-attention').hidden, false, 'a notice alone keeps the shared section visible');
  assert.match(c.element('needs-you-slot').innerHTML, /warning-0/);
  c.CT.health.warnings = []; c.render();
  assert.equal(c.element('home-attention').hidden, true);
});

test('home preserves collector priority, displays partial items, and removes resolved items', () => {
  const c = homePage();
  const critical = attentionItem({ id: 'critical', kind: 'order_unknown', title: 'Unknown <script>', priority: 1,
    link: { page: 'activity', params: { subject: 'order A&1' } } });
  c.CT.attention = attentionPayload([critical, attentionItem()], false); c.render();
  const html = c.element('needs-you-slot').innerHTML;
  assert.equal(c.element('home-attention').hidden, false);
  assert.match(html, /Unknown &lt;script&gt;/); assert.match(html, /activity.html\?subject=order\+A%261/);
  assert.ok(html.indexOf('data-attention-id="critical"') < html.indexOf('waiting:A'));
  assert.match(html, /at.more 1/); assert.match(html, /at.unknown/);
  c.element('attention-rest').open = true; c.render(); assert.equal(c.element('attention-rest').open, true);
  c.CT.attention = attentionPayload([attentionItem()]); c.render();
  assert.doesNotMatch(c.element('needs-you-slot').innerHTML, /Unknown/);
  c.CT.attention = attentionPayload(); c.render();
  assert.equal(c.element('home-attention').hidden, true);
  assert.equal(c.element('needs-you-slot').innerHTML, '');
  c.CT.attention = attentionPayload([attentionItem()]); c.render();
  assert.equal(c.element('home-attention').hidden, false, 'a new item restores the section after an empty poll');
});

test('task links only allow known pages and parameter keys, encoding every value', () => {
  const c = context(); c.run('console.js');
  assert.equal(c.Console.taskLink({ page: '//evil.test', params: {} }), null);
  assert.equal(c.Console.taskLink({ page: '__proto__', params: {} }), null);
  assert.equal(c.Console.taskLink({ page: 'research', params: { href: 'javascript:alert(1)' } }), null);
  assert.equal(c.Console.taskLink({ page: 'research', params: { run: 'A&scope=agentic' } }), 'research.html?run=A%26scope%3Dagentic');
  const home = homePage();
  home.CT.attention = attentionPayload([attentionItem({ kind: 'personal_task_done', subject: 'review-A',
    link: { page: 'advisor', params: { scope: 'personal' } } })]); home.render();
  assert.match(home.element('needs-you-slot').innerHTML, /research.html\?run=review-A/);
});

test('More only offers approvals for collector items requiring a proposal decision', () => {
  const c = context(true); c.run('console.js'); c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.CT_API = { authenticated: true, state: {}, isLive: () => true };
  c.run('pages/more.js');
  c.CT.attention = attentionPayload([attentionItem({ kind: 'config_change_pending', requires_person: false })]); c.render();
  assert.equal(c.element('more-approvals').hidden, true);
  c.CT.attention = attentionPayload([attentionItem()]); c.render();
  assert.equal(c.element('more-approvals').hidden, false);
  assert.match(c.element('more-approvals').href, /approvals.html\?proposal=A/);
  const unfinished = attentionItem({ kind: 'exit_incomplete', actions: ['continue_exit', 'open'],
    link: { page: 'approvals', params: { proposal: 'exit-root' } } });
  c.CT.attention = attentionPayload([unfinished]); c.render();
  assert.equal(c.element('more-approvals').hidden, false);
  assert.match(c.element('more-approvals').href, /proposal=exit-root/);
  const home = homePage(); home.CT.attention = attentionPayload([unfinished]); home.render();
  assert.match(home.element('needs-you-slot').innerHTML, /proposal=exit-root/);
  c.CT.attention.readable = false; c.render();
  assert.equal(c.element('more-attention-copy').textContent, 'at.unknown');
});

test('reading or polling a finished run never marks it opened; explicit receipts deduplicate with CSRF', async () => {
  const c = context(); const writes = [];
  c.fetch = async (url, options) => {
    if (url === '/auth/session') return response({ authenticated: true, csrf_token: 'test-token' });
    if (options.method === 'POST') { writes.push({ url, options }); return response({ run_key: 'done', opened_at: new Date().toISOString() }); }
    if (url === '/attention') return response(attentionPayload());
    return response({ run_key: 'done', state: 'COMPLETE' });
  };
  c.run('api.js'); c.CT_API.start(['researchRun']); c.CT_API.watchRun('done'); await flush();
  await c.CT_API.readOnce('researchRun'); assert.equal(writes.length, 0);
  await Promise.all([c.CT_API.markOpened('done'), c.CT_API.markOpened('done')]);
  await c.CT_API.markOpened('done');
  assert.equal(writes.length, 1); assert.equal(writes[0].url, '/research/done/opened');
  assert.equal(writes[0].options.headers['X-CSRF-Token'], 'test-token');
  c.document.hidden = true; await c.CT_API.markOpened('hidden'); assert.equal(writes.length, 1);
});

function researchPage(fetchJSON, options = {}) {
  const c = context(); c.run('console.js');
  if (options.search) c.location.search = options.search;
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.element('[data-od-id="confidence-chart"]').before = () => {};
  c.element('f-bucket').options = []; c.element('f-bucket').value = 'micro_live';
  c.opened = [];
  c.CT_API = { activeRun: null, writesEnabled: true, watchRun: () => {}, readOnce: async () => {}, fetchJSON,
    state: { resources: { researchHistory: { lastOkAt: Date.now() } } }, isLive: () => true,
    matchesResearch: () => true, selectResearch: () => {},
    markOpened: async key => { c.opened.push(key); return { opened_at: 'now' }; } };
  c.run('pages/research.js');
  c.openReport = run => c.document.dispatchEvent({ type: 'click', preventDefault() {}, target: {
    closest: selector => selector === '[data-trace]' ? { getAttribute: () => run } : null } });
  return c;
}

test('research filters isolate unknown, other-account and all-account records unless explicitly included', () => {
  const c = context(true); c.run('api.js');
  const own = { scope: 'personal', account_scope_kind: 'single', account_id: 'A', task_type: 'analysis' };
  const all = { scope: 'personal', account_scope_kind: 'all_personal', account_ids: ['A', 'B'], task_type: 'portfolio_review' };
  c.CT_API.selectResearch({ scope: 'personal', account: 'A' });
  assert.equal(c.CT_API.matchesResearch(own), true);
  assert.equal(c.CT_API.matchesResearch({ ...own, account_id: 'B' }), false);
  assert.equal(c.CT_API.matchesResearch({ ...own, account_scope_kind: 'unknown' }), false);
  assert.equal(c.CT_API.matchesResearch(all), false);
  c.CT_API.selectResearch({ scope: 'personal', account: 'A', include_all_personal: true });
  assert.equal(c.CT_API.matchesResearch(all), true);
  c.CT_API.selectResearch({ scope: 'personal', account: 'A', include_all_personal: true, task_type: 'analysis' });
  assert.equal(c.CT_API.matchesResearch(all), false);
  assert.equal(c.CT_API.matchesResearch(all, false), true);
  assert.equal(c.CT_API.matchesResearch({ ...own, scope: 'unspecified' }), false);
});

test('scoped research requests discard old account responses without changing shared home and lab resources', async () => {
  const c = context(); const pending = new Map(); c.fetch = url => new Promise(resolve => pending.set(url, resolve));
  c.run('api.js'); c.CT.research = [{ run_id: 'home' }]; c.CT.researchJobs = [{ run_key: 'lab' }];
  c.CT_API.selectResearch({ scope: 'personal', account: 'A' });
  const first = [...pending.keys()];
  c.CT_API.selectResearch({ scope: 'personal', account: 'B', task_type: 'analysis' });
  first.forEach(path => pending.get(path)(response([{ scope: 'personal', account_scope_kind: 'single', account_id: 'A', task_type: 'analysis' }])));
  await flush(); assert.equal(c.CT.researchHistory.length, 0); assert.equal(c.CT.researchQueue.length, 0);
  const current = [...pending.keys()].filter(path => path.includes('account_id=B'));
  assert.equal(current.length, 2); assert.ok(current.some(path => path.includes('task_type=analysis')));
  assert.ok(current.find(path => path.includes('/jobs')).includes('task_type=') === false);
  current.forEach(path => pending.get(path)(response([{ scope: 'personal', account_scope_kind: 'single', account_id: 'B', task_type: 'analysis' }])));
  await flush(); assert.equal(c.CT.researchHistory[0].account_id, 'B');
  assert.equal(c.CT.research[0].run_id, 'home'); assert.equal(c.CT.researchJobs[0].run_key, 'lab');
});

test('research rejects a server response for the wrong account rather than reporting an empty success', async () => {
  const c = context(); c.fetch = async () => response([{ scope: 'personal', account_scope_kind: 'single', account_id: 'B' }]);
  c.run('api.js'); c.CT_API.selectResearch({ scope: 'personal', account: 'A' }); await flush();
  assert.equal(c.CT.researchHistory.length, 0); assert.equal(c.CT_API.isLive('researchHistory'), false);
  assert.equal(c.CT_API.state.resources.researchHistory.error, 'rs.recordsInvalid');
});

test('research exposes provenance, unknown fact time and supersession without promoting an old result', async () => {
  const c = researchPage(async url => url.endsWith('/trace') ? { run_id: 'old', final_action: 'HOLD', steps: [] }
    : { run_key: 'old', state: 'COMPLETE' });
  const old = { run_id: 'old', ticker: 'OLD', scope: 'agentic', account_label: 'Agentic', account_scope_kind: 'single',
    task_type: 'full_research', route: { name: 'tradingagents', inferred: true, models: { quick: 'model-A' } },
    sources_retrieved_at_max: '2026-09-18T20:00:00Z', facts_as_of: null, counter_evidence: '<script>bad</script>',
    counter_evidence_truncated: true, superseded_by: 'new', limitations: { missing_information: ['cash flow'] } };
  c.CT.researchHistory = [old, { ...old, run_id: 'new', ticker: 'NEW', superseded_by: null }]; c.render();
  const html = c.element('panel-runs-all').innerHTML;
  assert.match(html, /rs.inferred/); assert.match(html, /model-A/); assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /NaN|undefined/);
  assert.match(html, /rs.meta.facts<\/dt><dd[^>]*>rs.unknown/); assert.match(html, /data-trace="new"/);
  assert.match(c.element('recent-card').innerHTML, /NEW/); assert.doesNotMatch(c.element('recent-card').innerHTML, /OLD/);
  c.openReport('old'); await flush(); const displayed = c.element('trace-record').innerHTML;
  c.CT.researchHistory = []; c.render(); assert.equal(c.element('trace-record').innerHTML, displayed);
});

test('personal, unknown and account-only links cannot start trading research', async () => {
  for (const search of ['?scope=personal', '?scope=unspecified', '?scope=invalid', '?account=A']) {
    const c = researchPage(async () => ({}), { search }); let posts = 0;
    c.CT_API.post = async () => { posts++; return {}; };
    assert.equal(c.element('[data-od-id="start-run"]').hidden, true);
    await c.element('run-form').events.submit({ preventDefault() {} }); assert.equal(posts, 0);
  }
});

test('research submissions bind scope and purpose and reuse the request id after an uncertain failure', async () => {
  const c = researchPage(async () => ({}), { search: '?scope=demo' }); const sent = [];
  c.element('f-ticker').value = 'vti'; c.element('f-purpose').value = 'review_holding';
  c.CT_API.post = async (path, body) => { sent.push({ ...body }); if (sent.length === 1) throw new Error('network'); return { run_key: 'new' }; };
  await c.element('run-form').events.submit({ preventDefault() {} });
  await c.element('run-form').events.submit({ preventDefault() {} });
  assert.equal(sent.length, 2); assert.equal(sent[0].scope, 'demo'); assert.equal(sent[0].bucket, 'core');
  assert.equal(sent[0].purpose, 'review_holding'); assert.equal(sent[0].ticker, 'VTI');
  assert.match(sent[0].request_id, /^[0-9a-f-]{36}$/); assert.equal(sent[0].request_id, sent[1].request_id);
  await c.element('run-form').events.submit({ preventDefault() {} }); assert.notEqual(sent[1].request_id, sent[2].request_id);
});

test('changing research scope closes the report and ignores its late response without marking it read', async () => {
  const pending = new Map(); const c = researchPage(url => new Promise(resolve => pending.set(url, resolve)));
  c.openReport('old'); c.location.search = '?scope=personal'; c.location.href = 'https://console.test/ui/research.html?scope=personal';
  c.document.dispatchEvent({ type: 'ct:route' });
  pending.get('/research/old/trace')({ run_id: 'old', final_action: 'SELL', steps: [] });
  pending.get('/research/old')({ run_key: 'old', state: 'COMPLETE' }); await flush();
  assert.equal(c.element('trace-sheet').open, false); assert.deepEqual(c.opened, []);
  assert.doesNotMatch(c.element('trace-steps').innerHTML, /SELL/);
});

test('an account-bound report deep link refuses another account result without consuming its notice', async () => {
  const c = researchPage(async url => url.endsWith('/trace') ? { run_id: 'wrong', final_action: 'SELL', steps: [] }
    : { run_key: 'wrong', scope: 'personal', account_id: 'B', state: 'COMPLETE' }, { search: '?scope=personal&account=A' });
  c.CT_API.matchesResearch = row => row?.scope === 'personal' && row.account_id === 'A';
  c.openReport('wrong'); await flush();
  assert.match(c.element('trace-steps').innerHTML, /rs.reportOutside/); assert.deepEqual(c.opened, []);
  assert.doesNotMatch(c.element('trace-steps').innerHTML, /SELL/);
});

test('a displayed report marks only its finished job, and shows failures before marking them', async () => {
  for (const state of ['COMPLETE', 'FAILED', 'RUNNING']) {
    const c = researchPage(async url => url.endsWith('/trace')
      ? { run_id: 'one', final_action: state === 'COMPLETE' ? 'HOLD' : null, steps: [] }
      : { run_key: 'one', state, error: state === 'FAILED' ? 'feed unavailable' : null });
    c.openReport('one'); await flush();
    assert.deepEqual(c.opened, state === 'RUNNING' ? [] : ['one']);
    if (state === 'FAILED') assert.match(c.element('trace-steps').innerHTML, /feed unavailable/);
    c.document.dispatchEvent({ type: 'ct:data' }); await flush();
    assert.equal(c.opened.length, state === 'RUNNING' ? 0 : 1);
  }
});

test('missing results and mismatched report ids remain unread', async () => {
  for (const trace of [{ run_id: 'one', steps: [] }, { run_id: 'other', final_action: 'SELL', steps: [] }]) {
    const c = researchPage(async url => url.endsWith('/trace') ? trace : { run_key: 'one', state: 'COMPLETE' });
    c.openReport('one'); await flush();
    assert.deepEqual(c.opened, []); assert.match(c.element('trace-steps').innerHTML, /rs.resultMissing/);
  }
});

test('late report responses cannot replace a newer report or consume its notification', async () => {
  const pending = new Map();
  const c = researchPage(url => new Promise(resolve => pending.set(url, resolve)));
  c.openReport('one'); c.openReport('two');
  pending.get('/research/two/trace')({ run_id: 'two', final_action: 'HOLD', steps: [] });
  pending.get('/research/two')({ run_key: 'two', state: 'COMPLETE' }); await flush();
  pending.get('/research/one/trace')({ run_id: 'one', final_action: 'SELL', steps: [] });
  pending.get('/research/one')({ run_key: 'one', state: 'COMPLETE' }); await flush();
  assert.deepEqual(c.opened, ['two']); assert.match(c.element('trace-head').innerHTML, /two/);
  assert.doesNotMatch(c.element('trace-head').innerHTML, /SELL/);
});

test('hidden and closed report sheets do not mark results before they are displayed', async () => {
  for (const close of [false, true]) {
    const pending = new Map(); const c = researchPage(url => new Promise(resolve => pending.set(url, resolve)));
    c.openReport('one'); c.document.hidden = true;
    if (close) c.element('trace-sheet').open = false;
    pending.get('/research/one/trace')({ run_id: 'one', final_action: 'HOLD', steps: [] });
    pending.get('/research/one')({ run_key: 'one', state: 'COMPLETE' }); await flush();
    assert.deepEqual(c.opened, []);
    c.document.hidden = false; c.document.dispatchEvent({ type: 'visibilitychange' }); await flush();
    assert.deepEqual(c.opened, close ? [] : ['one']);
  }
});

test('failed read receipts remain visible and are only retried explicitly', async () => {
  const c = researchPage(async url => url.endsWith('/trace') ? { run_id: 'one', final_action: 'HOLD', steps: [] }
    : { run_key: 'one', state: 'COMPLETE' });
  let attempts = 0;
  c.CT_API.markOpened = async () => { attempts++; throw new Error('offline'); };
  c.openReport('one'); await flush();
  assert.equal(attempts, 1); assert.match(c.element('trace-read-status').textContent, /offline/);
  c.document.dispatchEvent({ type: 'visibilitychange' }); c.document.dispatchEvent({ type: 'ct:data' }); await flush();
  assert.equal(attempts, 1);
  c.element('trace-read-retry').events.click(); await flush(); assert.equal(attempts, 2);
});

test('an expired proposal deep link is read directly without falling back to an unrelated queue item', async () => {
  const c = context(); c.CT.proposals = [{ proposal_id: 'active', version: 'v' }];
  c.fetch = async url => url === '/proposals' ? response(c.CT.proposals)
    : response({ proposal_id: 'expired', version: 'old', status: 'STALE' });
  c.run('api.js'); c.CT_API.selectProposal('expired', { includeClosed: true }); await flush();
  assert.equal(c.CT.proposalDetail.proposal_id, 'expired');
  await c.CT_API.readOnce('proposals'); assert.equal(c.CT.proposalDetail.proposal_id, 'expired');
  c.fetch = async () => response({ detail: 'not found' }, 400);
  c.CT_API.selectProposal('missing', { includeClosed: true }); await flush();
  assert.equal(c.CT.proposalDetail, null);
  assert.equal(c.CT_API.SOURCES.proposalDetail.path(), '/proposals/missing');
});

test('a linked waiting proposal is invalidated when its queue version changes or it leaves the queue', async () => {
  for (const updated of [[{ proposal_id: 'A', version: 'new' }], []]) {
    const c = context(); c.CT.proposals = [{ proposal_id: 'A', version: 'old' }];
    c.fetch = async url => url === '/proposals' ? response(updated)
      : response({ proposal_id: 'A', version: 'old', status: 'WAITING_FOR_APPROVAL' });
    c.run('api.js'); c.CT_API.selectProposal('A', { includeClosed: true }); await flush();
    assert.equal(c.CT.proposalDetail.proposal_id, 'A');
    await c.CT_API.readOnce('proposals');
    assert.equal(c.CT.proposalDetail, null);
    assert.equal(c.CT_API.isLive('proposalDetail'), false);
  }
});

test('Activity deep links filter orders and audit records and discard an older subject response', async () => {
  const c = context(); c.run('console.js'); c.Console.mount = c.Console.wireTabs = () => {};
  c.location.search = '?subject=A'; const pending = new Map();
  c.CT.orders = { fills: [], tracked_orders: [{ proposal_id: 'A', symbol: 'VTI' }, { proposal_id: 'B', symbol: 'BND' }] };
  c.CT_API = { fetchJSON: url => new Promise(resolve => pending.set(url, resolve)) };
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.run('pages/activity.js');
  assert.match(c.element('tracked-body').innerHTML, /VTI/); assert.doesNotMatch(c.element('tracked-body').innerHTML, /BND/);
  c.location.search = '?subject=B'; c.document.dispatchEvent({ type: 'ct:route' });
  pending.get('/activity?limit=25&subject=B')({ events: [{ id: 2, subject: 'B', kind: 'test', description: 'new subject' }], next_cursor: null });
  await flush();
  pending.get('/activity?limit=25&subject=A')({ events: [{ id: 1, subject: 'A', kind: 'test', description: 'old subject' }], next_cursor: null });
  await flush();
  assert.match(c.element('audit-list').innerHTML, /new subject/); assert.doesNotMatch(c.element('audit-list').innerHTML, /old subject/);
  assert.match(c.element('tracked-body').innerHTML, /BND/); assert.doesNotMatch(c.element('tracked-body').innerHTML, /VTI/);
});

test('Assistant marks a completed decision only while its result sheet remains visible', async () => {
  for (const leave of [false, true]) {
    let finish; const opened = [];
    const c = advisorPage(async () => ({}), { fetchJSON: url => url.startsWith('/research/')
      ? new Promise(resolve => { finish = resolve; }) : Promise.resolve({ messages: [] }) });
    c.CT_API.markOpened = async key => { opened.push(key); };
    c.CT.advisor.decisions = [{ run_id: 'personal', ticker: 'VTI', action: 'HOLD', bucket: 'advisory', policy: { proposal: {} } }]; c.render();
    c.document.dispatchEvent({ type: 'click', target: { closest: selector => selector === '[data-decision]'
      ? { dataset: { decision: 'personal' } } : null } });
    if (leave) c.document.dispatchEvent({ type: 'ct:leave' });
    finish({ run_key: 'personal', state: 'COMPLETE' }); await flush();
    assert.deepEqual(opened, leave ? [] : ['personal']);
  }
});

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

function profilePage(saved, options = {}) {
  const c = context(); c.run('console.js');
  c.Console.mount = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  const words = { 'pc.topic.goals': 'Goals', 'pc.topic.timeline': 'Timeline', 'pc.topicSep': ': ' };
  const t = c.I18N.t;
  c.I18N.t = (key, vars) => words[key] || t(key, vars);
  c.CT.advisor = { notes: saved };
  c.posted = [];
  c.CT_API = { authenticated: true, writesEnabled: true, reads: 0,
    readOnce: async () => { c.CT_API.reads++; },
    post: options.post || (async (url, body) => {
      c.posted.push({ url, body });
      return { text: body.text.trim(), revision: body.revision + 1, updated_at: '2026-09-23T10:00:00+00:00' };
    }) };
  const notes = c.element('profile-notes');
  notes.focus = () => { notes.focused = true; };
  notes.setSelectionRange = (from, to) => { notes.caret = [from, to]; };
  c.run('pages/profile.js');
  c.type = (value) => { notes.value = value; notes.events.input(); };
  c.tap = (topic) => c.element('profile-topics').events.click({ target: { closest: () => ({ dataset: { topic } }) } });
  return c;
}

test('personal context saves only a change, then shows what the server stored', async () => {
  const c = profilePage({ text: 'Saved plan', revision: 3, updated_at: '2026-09-20T14:32:00+00:00' });
  const notes = c.element('profile-notes'), save = c.element('profile-save');
  assert.equal(notes.value, 'Saved plan');
  assert.equal(save.disabled, true, 'nothing to save');
  assert.equal(c.element('profile-discard').hidden, true);
  assert.equal(c.element('profile-state').textContent, 'pc.saved');
  assert.match(c.element('profile-saved-at').textContent, /Sep 20/);
  c.type('Saved plan');
  assert.equal(save.disabled, true, 'typing back to the saved text is not a change');
  c.type('Saved plan, revised  \n');
  assert.equal(save.disabled, false);
  assert.equal(c.element('profile-state').textContent, 'pc.unsaved');
  assert.equal(c.element('profile-discard').hidden, false);
  assert.match(c.element('profile-count').textContent, / 22 4,000$/);
  await save.events.click();
  assert.equal(JSON.stringify(c.posted), JSON.stringify([{ url: '/advisor/notes', body: { text: 'Saved plan, revised  \n', revision: 3 } }]));
  assert.equal(notes.value, 'Saved plan, revised', 'the trimmed text the server stored');
  assert.equal(save.disabled, true);
  assert.equal(c.element('profile-state').textContent, 'pc.saved');
  assert.equal(c.element('profile-status').textContent, 'ai.saved');
  assert.equal(c.CT_API.reads, 1);
  // A poll that left before the save answered still carries revision 3.
  c.render();
  assert.equal(notes.value, 'Saved plan, revised', 'an older reading never restores the text it replaced');
  assert.equal(c.element('profile-status').textContent, 'ai.saved', 'nor reads as another device');
  c.type('Saved plan, revised again');
  c.render();
  assert.equal(c.element('profile-status').textContent, '', 'not even while a new draft is open');
  assert.equal(notes.value, 'Saved plan, revised again');
});

test('a newer save from another device never replaces a draft; discarding loads it', async () => {
  const c = profilePage({ text: 'Old', revision: 2 });
  const notes = c.element('profile-notes');
  c.type('Old, with my edit');
  c.CT.advisor.notes = { text: 'From the phone', revision: 3 };
  c.render();
  assert.equal(notes.value, 'Old, with my edit');
  assert.equal(c.element('profile-status').textContent, 'pc.newer');
  await c.element('profile-discard').events.click();
  assert.equal(notes.value, 'From the phone');
  assert.equal(c.element('profile-save').disabled, true);
  assert.equal(c.element('profile-status').textContent, '');
  assert.equal(c.CT_API.reads, 1, 'and asks for anything newer still');
  c.CT.advisor.notes = { text: 'Newest', revision: 4 };
  c.render();
  assert.equal(notes.value, 'Newest', 'without a draft, a newer save simply shows');
});

test('a refused save keeps the draft, quotes the API and says how to recover', async () => {
  const c = profilePage({ text: 'Old', revision: 2 }, { post: async () => {
    throw Object.assign(new Error('Personal notes changed on another device; reload before saving.'), { status: 409 });
  } });
  c.type('Mine');
  await c.element('profile-save').events.click();
  const status = c.element('profile-status');
  assert.equal(c.element('profile-notes').value, 'Mine');
  assert.match(status.textContent, /changed on another device/);
  assert.match(status.textContent, /pc\.conflictHint/);
  assert.match(status.className, /is-error/);
  c.type('Mine, still');
  assert.match(status.textContent, /pc\.conflictHint/, 'an error stays until it is acted on');
  assert.equal(c.element('profile-save').disabled, false, 'and the save can be tried again');
});

test('a topic writes its heading on a new line once, then only goes to it', () => {
  const c = profilePage({ text: 'Goals: a house deposit', revision: 1 });
  const notes = c.element('profile-notes');
  c.tap('timeline');
  assert.equal(notes.value, 'Goals: a house deposit\nTimeline: ');
  assert.deepEqual(notes.caret, [notes.value.length, notes.value.length]);
  assert.equal(notes.focused, true);
  assert.equal(c.element('profile-hint').hidden, false);
  assert.match(c.element('profile-hint').innerHTML, /pc\.hint\.timeline/);
  assert.equal(c.element('profile-save').disabled, false);
  c.tap('goals');
  assert.equal(notes.value, 'Goals: a house deposit\nTimeline: ', 'already written: nothing is added');
  assert.deepEqual(notes.caret, [22, 22], 'the caret goes to the end of that line');
  const other = profilePage({ text: '  GOALS：稳', revision: 1 });
  other.tap('goals');
  assert.equal(other.element('profile-notes').value, '  GOALS：稳', 'any case, either colon');
  const full = profilePage({ text: 'x'.repeat(3995), revision: 1 });
  full.tap('timeline');
  assert.equal(full.element('profile-notes').value.length, 3995, 'a heading never pushes past the limit');
  assert.equal(full.element('profile-status').textContent, 'pc.full');
});

test('signing out empties the personal context and cannot save it', () => {
  const c = profilePage({ text: 'Private', revision: 1 });
  c.type('Private, edited');
  c.CT.advisor.notes = { text: 'Private, from the phone', revision: 2 };
  c.render();
  assert.match(c.element('profile-status').className, /is-error/);
  Object.assign(c.CT_API, { authenticated: false, writesEnabled: false });
  c.render();
  assert.equal(c.element('profile-notes').value, '');
  assert.equal(c.element('profile-save').disabled, true);
  assert.equal(c.element('profile-state').textContent, '');
  assert.equal(c.element('profile-status').textContent, '');
  assert.equal(c.element('profile-status').className, 'pc-status', 'no warning is left behind for the next sign-in');
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
     effect was the opposite. /micro-live then opened a fresh Robinhood OAuth/MCP session on
     every call, and /orders did the same whenever micro-live execution was enabled; each
     handshake ran for four to eight seconds and delayed every other read while it did. At 15s
     the API was handshaking almost continuously, and a tab change waited seconds for a SQLite
     read that costs 26 ms on its own. Nothing failed, because nothing here was watching. The
     API now keeps its venue session and its costliest readings between polls, which makes a
     poll cheap; it does not make a quicker table free.

     So these are pinned to FRONTEND_HANDOFF.md §15 and to what ui/README.md tells the reader.
     Quickening one is a decision about the whole console: change the budget, this test and the
     README together, having looked at what the endpoint costs the API. */
  const src = uiFile('assets/api.js');
  const found = Object.fromEntries([...src.matchAll(/^\s{4}(\w+): \{[^}]*?ms: (\d+)/gm)]
    .map(m => [m[1], Number(m[2])]));
  const budget = {
    advisor: 5000, assistant: 5000, health: 30000, schedule: 60000, operations: 60000,
    notifications: 120000, pushDevices: 120000, authSessions: 60000, config: 60000, authority: 30000, attention: 30000, evaluation: 300000,
    portfolio: 60000, risk: 60000, microLive: 60000, proposals: 15000, orders: 10000,
    research: 60000, researchHistory: 60000, researchQueue: 10000, accounts: 60000, labRoster: 900000, researchJobs: 10000, researchRun: 5000,
    proposalDetail: 15000,
  };
  assert.deepEqual(found, budget);

  // /micro-live reads the venue whenever its kept reading is due; it may not be quickened.
  assert.ok(found.microLive >= 60000, '/micro-live renews its venue reading through the poll');

  // A threshold shorter than the API's own serving window plus a poll would call a reading the
  // API is right to serve out of date (service/__init__.py, the Kept readings in Service).
  const stale = Object.fromEntries([...src.matchAll(/^\s{4}(\w+): \{[^}]*?staleAfter: (\d+)/gm)]
    .map(m => [m[1], Number(m[2])]));
  assert.ok(stale.portfolio >= 480000 + found.portfolio, 'portfolio is served for up to eight minutes');
  assert.ok(stale.microLive >= 90000 + found.microLive, 'micro-live is served for up to ninety seconds');
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
  const proposals = members('ProposalStatus'), systems = members('SystemStatus');
  assert.ok(proposals.includes('WAITING_FOR_APPROVAL') && systems.includes('READY'), 'the enums were read');
  const wanted = [...proposals.map(s => 'ps.' + s), ...systems.map(s => 'sys.' + s),
                  'sys.UNKNOWN'];            // what systemStatus() answers while /health is not live
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

test('every console page off the tab bar has a back control and lights its section tab', () => {
  /* The tab bar only returns you to a section root. A page missing from PARENT gets the brand
     where its back control belongs, and one missing from NAV lights no tab: Settings shipped
     with both. The assistant is full screen and has its own #assistant-back. */
  const routes = [...uiFile('assets/navigation.js').match(/const routes = new Set\(\[([^\]]*)\]\)/)[1]
    .matchAll(/'(\w+)'/g)].map(m => m[1]);
  const c = context(); c.run('console.js');
  const { NAV, PARENT } = c.Console;
  assert.ok(routes.includes('settings'));
  for (const route of routes.filter(route => route !== 'more')) {
    const nav = NAV.find(n => n.href === route + '.html');
    assert.ok(nav, `${route} is not in NAV`);
    if (!nav.tab && route !== 'advisor') assert.ok(PARENT[route], `${route} has no back control`);
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

test('the sign-in screen keeps the office illustration visually quiet', () => {
  const html = uiFile('login.html');
  assert.doesNotMatch(html, /login\.subtitle|login\.setup|login\.office|个人控制台|首次使用请/);
  assert.doesNotMatch(html, /data-i18n|login-back|class="topnav"|取消|登录/);
  assert.match(html, /<h1>Sign in<\/h1>/);
  const login = uiFile('assets/login.js');
  assert.doesNotMatch(login, /login-setup|login\.setup|common\.cancel|applyStatic|login-back/);
  assert.match(login, /Signing in/);
  const office = uiFile('assets/lab/login-office.js');
  assert.doesNotMatch(office, /login\.office|signing in to/);
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

test('a run queued as "auto" names no instrument until the engine has classified it', () => {
  // Scheduled, manual and reanalysis runs are all queued as "auto". Until the run starts
  // nothing has said which analysts will sit, so none is drawn as skipped.
  const job = { run_key: 'q1', ticker: 'VTI', trade_date: '2026-09-10', instrument_type: 'auto', state: 'QUEUED' };
  const c = labPage({ jobs: [job] });
  assert.ok(Object.values(deskStates(c)).every((s) => s === 'waiting'));
  assert.equal(c.element('#instrument').hidden, true);
  assert.equal(c.element('#team-count').textContent, '');

  // The start event carries the engine's answer, and the room follows it.
  job.state = 'RUNNING';
  c.CT.researchRun = { run_key: 'q1', state: 'RUNNING', progress: {
    instrument_type: 'etf', analysts: ['market', 'news'], desks: {}, result: null, events: [],
    last_event_at: new Date().toISOString() } };
  c.render();
  const states = deskStates(c);
  assert.equal(states.fundamentals, 'disabled');
  assert.equal(states.market, 'waiting');
  assert.equal(c.element('#instrument').hidden, false);
  assert.equal(c.element('#instrument').textContent, 'etf');
  assert.doesNotMatch(c.element('#roster').innerHTML, /auto/);
});

test('a finished run shows the instrument its record names, not the one its job asked for', async () => {
  const trace = { run_id: 'r1', ticker: 'VTI', instrument_type: 'etf', analysts: ['market', 'news'],
                  policy_action: 'NO_TRADE', steps: [] };
  const c = labPage({
    jobs: [{ run_key: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'auto', state: 'COMPLETE' }],
    research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'etf',
                 policy_action: 'NO_TRADE' }],
    fetchJSON: async () => trace });
  await flush();
  c.render();

  assert.equal(c.element('#instrument').textContent, 'etf');
  assert.match(c.element('#roster').innerHTML, /lab\.skip\.generic etf/);
  assert.doesNotMatch(c.element('#roster').innerHTML, /auto/);
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
  assert.equal(c.element('.lab-demo-note').dataset.kind, 'error', 'a refusal reads as a fault, not as demo data');
});

test('the run capsule names the run on screen, and a settled run ends on when the next is due', async () => {
  const trace = { run_id: 'r1', ticker: 'VTI', instrument_type: 'etf', analysts: ['market', 'news'],
                  policy_action: 'NO_TRADE', steps: [] };
  const c = labPage({ research: [{ run_id: 'r1', ticker: 'VTI', trade_date: '2026-09-04', instrument_type: 'etf',
                                   policy_action: 'NO_TRADE', final_action: 'NO_TRADE' }],
                      fetchJSON: async () => trace });
  await flush();
  c.render();

  assert.equal(c.element('.lab-run').hidden, false);
  assert.equal(c.element('#run-orb').textContent, 'V');
  // The ticker heads the capsule, so the line under it is the date and the outcome only.
  assert.match(c.element('#run-label').textContent, /^Sep 4, 2026 · No Trade$/);
  assert.equal(c.element('#mode-badge').dataset.mode, 'done');
  // Today's budget is in the head; the settled run's own card only says when the next is due.
  assert.equal(c.element('#rest-note').hidden, false);
  assert.match(c.element('#rest-note').textContent, /^lab\.nextAt /);

  const empty = labPage({ research: [], jobs: [] });
  assert.equal(empty.element('.lab-run').hidden, true, 'with nothing to pick the picker goes');
  assert.equal(empty.element('#mode-badge').dataset.mode, 'none');
  assert.equal(empty.element('#rest-note').hidden, true, 'the caption already names the next run');
});

test('a desk picked from the list brings its card into view; one picked in the office does not', () => {
  const c = labPage({ research: [], jobs: [] });
  const scrolls = [];
  c.element('#desk-card').scrollIntoView = (options) => scrolls.push(options);
  const pick = (role, className) => c.document.dispatchEvent({ type: 'click', target: {
    closest: (selector) => selector === '[data-role]'
      ? { dataset: { role }, classList: { contains: (name) => name === className } } : null } });

  pick('news', 'person');
  assert.equal(c.labPixelScene.synced.selected, 'news');
  assert.equal(scrolls.length, 0, 'the office sits right above the card');
  pick('bear', 'role-button');
  assert.equal(c.labPixelScene.synced.selected, 'bear');
  assert.deepEqual(scrolls.map((o) => [o.block, o.behavior]), [['nearest', 'auto']],
                   'reduced motion jumps rather than glides');
});

test('the Lab keeps its own colours: only its words and buttons turn ink', () => {
  /* The owner's rule, 2026-09-23. A redesign once drew the office, its people and their
     portraits in grey to match the console's ink. The room is drawn in the colours it was
     painted with, and so are the portraits; the type and the buttons are what turn ink. */
  const css = uiFile('assets/lab.css'), html = uiFile('lab.html');
  assert.doesNotMatch(css, /grayscale|saturate\(0/);
  assert.doesNotMatch(html, /data-palette|grayscale/);
  assert.match(uiFile('assets/lab/pixel-office.js'), /floor:'#e9e5da'/);
  assert.match(uiFile('assets/pages/lab.js'), /hue-rotate\(/, 'the second six keep their own colouring in the portrait');
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
  c.CT.attention = null;
  c.CT_API.watchRun = key => { c.CT_API.activeRun = key; };
  c.CT.researchJobs = [{ run_key: '2026-09-15_VTI_test', state: 'RUNNING' }]; c.render();
  assert.equal(c.CT_API.activeRun, '2026-09-15_VTI_test');
  assert.match(c.element('agent-state').textContent, /agent.studying/);
  c.CT.researchJobs = []; c.CT.portfolio = { accounts: {}, buckets: {}, errors: { micro_live: 'unconfirmed' } };
  c.CT.proposals = []; c.render();
  assert.equal(c.CT_API.activeRun, null);
  assert.equal(c.element('hwm-delta').textContent, '—');
  assert.match(c.element('needs-you-slot').innerHTML, /at.unknown/, 'only the attention collector decides what needs action');
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
  c.element('login-form').elements = { username: { value: 'owner' }, password: { value: 'pw' } };
  let assigned = null;
  c.location.assign = url => { assigned = url; };
  c.fetch = async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected token <'); } });
  c.run('login.js');
  await c.element('login-form').events.submit({ preventDefault() {} });
  assert.equal(c.element('login-result').textContent, 'Sign-in did not complete (HTTP 502).');
  assert.equal(c.element('login-submit').disabled, false);
  assert.equal(assigned, null);
});

/* Signing in from another tab replaces the session cookie. This tab kept its old CSRF token, and
   every write it made was refused with 403 until the tab was hidden and shown again. */
test('a stale CSRF token re-reads the session, marks the write not sent, and never resends it', async () => {
  const c = context(); let token = 'old', posts = 0, sessions = 0;
  c.fetch = async (url, init = {}) => {
    if (url === '/auth/session') { sessions++; return response({ authenticated: true, csrf_token: token }); }
    if (init.method === 'POST') {
      posts++;
      return init.headers['X-CSRF-Token'] === token ? response({ status: 'SUBMITTED' })
        : response({ detail: 'invalid session or CSRF token' }, 403);
    }
    return response([]);
  };
  c.run('api.js'); c.CT_API.start(['proposals']); await flush();
  token = 'new';                                    // another tab signed in
  const before = sessions;
  const error = await c.CT_API.post('/proposals/A/approve', {}).catch(e => e);
  assert.equal(error.notSent, true);
  assert.equal(posts, 1, 'the refused write is not sent again');
  await flush();
  assert.equal(sessions, before + 1, 'the session is read again at once');
  assert.equal((await c.CT_API.post('/proposals/A/approve', {})).status, 'SUBMITTED');
});

test('an approval the API refused before sending is a plain failure, not an uncertain order', async () => {
  const c = approvalPage();
  c.CT_API.post = async () => { throw Object.assign(new Error('invalid session or CSRF token'), { notSent: true }); };
  c.element('btn-approve').events.click();
  c.element('approve-confirm').checked = true; c.element('approve-confirm').events.change();
  await c.element('approve-submit').events.click();
  assert.match(c.element('result-slot').innerHTML, /ap.failed/);
  assert.equal(c.element('btn-approve').disabled, false, 'nothing to reconcile, so approving again is allowed');

  const timedOut = approvalPage();
  timedOut.CT_API.post = async () => { throw new Error('Request timed out. Check the order state before retrying.'); };
  timedOut.element('btn-approve').events.click();
  timedOut.element('approve-confirm').checked = true; timedOut.element('approve-confirm').events.change();
  await timedOut.element('approve-submit').events.click();
  assert.match(timedOut.element('result-slot').innerHTML, /ap.checkOrderTitle/);
  assert.equal(timedOut.element('btn-approve').disabled, true);
});

/* ── settings: config edits go through one preview-then-confirm sheet ───── */
function settingsPage(overrides, preview) {
  const c = context(true); c.run('console.js');
  Object.assign(c.CT, overrides || {});
  c.Console.mount = c.Console.wireTabs = () => {};
  c.Console.live = (keys, render) => { c.render = render; render(); };
  c.Console.openSheet = id => { c.element(id).open = true; };
  c.Console.closeSheet = id => { c.element(id).open = false; };
  c.posted = [];
  c.CT_API = { canAct: () => true, isLive: () => true, writesEnabled: true, authenticated: true,
    readOnce: async () => {}, fetchJSON: async () => ({ text: 'x: 1', revision: 'r', guarded: false }),
    post: async (url, body) => {
      c.posted.push({ url, body });
      if (url === '/config/preview') return preview || { changed: true, classification: 'immediate', diff: '-a\n+b', reasons: [] };
      return { status: 'applied', outcome: { git: 'committed abc' } };
    } };
  c.run('pages/settings.js');
  return c;
}

test('settings lists what is cooling, with a withdraw control, and every section by file', () => {
  const c = settingsPage();
  assert.equal(c.element('pending-section').hidden, false);
  const pending = c.element('pending-list').innerHTML;
  assert.ok(pending.includes('data-cancel="demo1"') && pending.includes('cf.withdraw'));
  assert.ok(pending.includes('micro_live_execution.max_capital_usd'));
  const files = c.element('file-list').innerHTML;
  assert.ok(files.includes('data-edit-key="micro_live_execution"') && files.includes('cf.guarded'));
  assert.ok(files.includes('event_calendar.yaml'));
  assert.equal(c.element('sc-hour').value, '13');
  assert.ok(c.element('wl-chips').innerHTML.includes('data-wl-remove="AAPL"'));
  assert.ok(c.element('recent-list').innerHTML.includes('committed abc') || c.element('recent-list').innerHTML.includes('applied'));
});

test('a loosening change previews, then needs the password before it is sent', async () => {
  const c = settingsPage({}, { changed: true, classification: 'cooling', diff: '-  max_capital_usd: 2000\n+  max_capital_usd: 2500',
    reasons: ['micro_live_execution.max_capital_usd'] });
  c.element('sc-hour').value = '14';
  c.element('sc-save').events.click();
  await flush();
  assert.equal(c.posted[0].url, '/config/preview');
  assert.equal(JSON.stringify(c.posted[0].body.set), JSON.stringify({ 'research_schedule.launchd.hour': 14 }));
  assert.equal(c.posted[0].body.revision, 'demo-risk');
  assert.equal(c.element('confirm-dialog').open, true);
  assert.equal(c.element('confirm-password-field').hidden, false);
  assert.ok(c.element('confirm-diff').innerHTML.includes('diff-add'));
  c.element('confirm-submit').events.click();
  await flush();
  assert.equal(c.posted.length, 1, 'nothing is sent without the password');
  assert.equal(c.element('confirm-status').textContent, 'cf.passwordNeeded');
  c.element('confirm-password').value = 'pw';
  c.element('confirm-submit').events.click();
  await flush();
  assert.equal(c.posted[1].url, '/config/changes');
  assert.equal(c.posted[1].body.password, 'pw');
  assert.equal(JSON.stringify(c.posted[1].body.set), JSON.stringify({ 'research_schedule.launchd.hour': 14 }));
  assert.equal(c.element('confirm-dialog').open, false);
});

test('a tightening change previews without a password field and sends on confirm', async () => {
  const c = settingsPage();
  c.element('wl-chips').events.click({ target: { closest: () => ({ dataset: { wlRemove: 'AAPL' } }) } });
  c.element('wl-save').events.click();
  await flush();
  assert.equal(JSON.stringify(c.posted[0].body), JSON.stringify({ file: 'investor_profile.yaml', revision: 'demo-investor',
    set: { 'universe.core.individual_stocks.watchlist': [] } }));
  assert.equal(c.element('confirm-password-field').hidden, true);
  c.element('confirm-submit').events.click();
  await flush();
  assert.equal(c.posted[1].url, '/config/changes');
  assert.equal(c.posted[1].body.password, undefined);
});

test('withdrawing a cooling change posts its cancel and nothing else', async () => {
  const c = settingsPage();
  const button = { disabled: false, dataset: { cancel: 'demo1' }, textContent: '' };
  c.element('pending-list').events.click({ target: { closest: () => button } });
  await flush();
  assert.equal(JSON.stringify(c.posted), JSON.stringify([{ url: '/config/changes/demo1/cancel', body: {} }]));
});

test('assistant account switches retain independent drafts and reject a late conversation', async () => {
  let late; const sent = [];
  const c = advisorPage(async (_, body) => { sent.push(body); return {}; }, { fetchJSON: url =>
    url.endsWith('/default') ? new Promise(resolve => { late = resolve; }) : Promise.resolve({ conversation_id: url.split('/').at(-1), messages: [] }) });
  c.element('chat-input').value = 'All accounts draft'; c.element('chat-input').events.input();
  const switchTo = search => { c.location.search = search; c.document.dispatchEvent({ type: 'ct:route' }); };
  switchTo('?scope=personal&account=aaaaaaaaaaaaaaaaaaaa'); await flush();
  assert.equal(c.element('chat-input').value, '');
  c.element('chat-input').value = 'Account A draft'; c.element('chat-input').events.input();
  late({ conversation_id: 'default', scope: 'personal', account_scope_kind: 'all_personal', messages: [{ role: 'assistant', content: 'Wrong account secret' }] }); await flush();
  assert.doesNotMatch(c.element('chat-messages').innerHTML, /Wrong account secret/);
  switchTo('?scope=personal&account=bbbbbbbbbbbbbbbbbbbb'); await flush();
  assert.equal(c.element('chat-input').value, '');
  switchTo('?scope=personal&account=aaaaaaaaaaaaaaaaaaaa'); await flush();
  assert.equal(c.element('chat-input').value, 'Account A draft');
  c.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(sent[0].scope, 'personal'); assert.equal(sent[0].account_id, 'aaaaaaaaaaaaaaaaaaaa');
  assert.notEqual(sent[0].conversation_id, 'default');
  switchTo('?scope=personal');
  assert.equal(c.element('chat-input').value, 'All accounts draft');
});

test('a mismatched conversation cannot show messages or accept another send', async () => {
  const sent = [];
  const c = advisorPage(async (_, body) => { sent.push(body); }, { search: '?scope=personal&account=aaaaaaaaaaaaaaaaaaaa&conversation=wrong',
    fetchJSON: async () => ({ conversation_id: 'wrong', scope: 'personal', account_scope_kind: 'single', account_id: 'bbbbbbbbbbbbbbbbbbbb', messages: [{ content: 'Private B' }] }) });
  await flush(); assert.equal(c.element('chat-send').disabled, true);
  assert.doesNotMatch(c.element('chat-messages').innerHTML, /Private B/);
  c.element('chat-input').value = 'Question'; c.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(sent.length, 0); assert.match(c.element('advisor-status').textContent, /ai.conversationMismatch/);
});

test('Agentic submits only chats, hides personal context, and preserves rejected drafts', async () => {
  const sent = [];
  const c = advisorPage(async (url, body) => { sent.push({ url, body }); const e = Error('Changed account'); e.status = 409; throw e; }, { search: '?scope=agentic' });
  await flush(); assert.equal(c.element('review-panel').hidden, true); assert.equal(c.element('view-discover').hidden, true);
  assert.equal(c.element('personal-notes').value, ''); assert.equal(c.element('notes-preview').textContent, '');
  c.element('shortlist').events.click(); c.element('review-start').events.click(); c.element('save-notes').events.click();
  c.element('symbol-input').value = 'VTI'; c.element('symbol-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(sent.length, 0);
  c.element('chat-input').value = 'Explain the research'; c.element('chat-form').events.submit({ preventDefault() {} }); await flush();
  assert.equal(sent.length, 1); assert.equal(sent[0].body.scope, 'agentic'); assert.equal(sent[0].body.account_id, null);
  assert.equal(c.element('chat-input').value, 'Explain the research'); assert.match(c.element('advisor-status').textContent, /ai.conflict/);
  c.render(); await flush(); assert.equal(sent.length, 1, 'polling must not retry a rejected write');
});

test('portfolio review needs an explicit start and receipt waits for the visible complete result', async () => {
  const sent = [], opened = [];
  const c = advisorPage(async (_, body) => { sent.push(body); return {}; }, { search: '?scope=personal&account=aaaaaaaaaaaaaaaaaaaa',
    fetchJSON: async url => url.startsWith('/research/') ? { run_key: 'review-a', state: 'COMPLETE' } : { conversation_id: url.split('/').at(-1), messages: [] } });
  c.CT_API.markOpened = async key => { opened.push(key); };
  c.CT.advisor.latest_review = { run_key: 'review-a', account_label: 'A', stale: true,
    snapshot_at: '2026-09-17', analysed_at: '2026-09-18', priorities: [{ symbol: 'VTI', rationale: '<script>test</script>', citations: [] }] };
  c.render(); await flush(); assert.equal(sent.length, 0); assert.equal(opened.length, 0);
  assert.match(c.element('review-stale').textContent, /ai.reviewStale/);
  await c.element('review-open').events.click(); await flush();
  assert.deepEqual(opened, ['review-a']); assert.match(c.element('decision-detail').innerHTML, /&lt;script&gt;/);
  c.render(); await flush(); assert.equal(opened.length, 1);
  c.element('review-start').events.click(); await flush();
  assert.equal(sent.length, 1); assert.equal(sent[0].kind, 'portfolio_review'); assert.equal(sent[0].account_id, 'aaaaaaaaaaaaaaaaaaaa');
});

test('scoped assistant resource rejects old and mismatched data without repointing Portfolio', async () => {
  const c = context(), calls = []; let release;
  c.fetch = async url => {
    calls.push(url);
    if (url === '/advisor?scope=personal') return new Promise(resolve => { release = resolve; });
    if (url === '/advisor?scope=agentic') return response({ scope: 'agentic', account_scope_kind: 'single', account_id: 'micro_live', jobs: [] });
    if (url.startsWith('/advisor?')) return response({ scope: 'personal', account_scope_kind: 'single', account_id: 'bbbbbbbbbbbbbbbbbbbb' });
    return response({ notes: { text: 'Portfolio source' } });
  };
  c.run('api.js'); c.CT_API.selectAssistant({ scope: 'personal' }); await flush();
  c.CT_API.selectAssistant({ scope: 'agentic' }); assert.equal(c.CT.assistant, null);
  release(response({ scope: 'personal', account_scope_kind: 'all_personal', account_id: null })); await flush();
  assert.equal(c.CT.assistant.scope, 'agentic');
  c.CT_API.selectAssistant({ scope: 'personal', account: 'aaaaaaaaaaaaaaaaaaaa' }); await flush();
  assert.equal(c.CT.assistant, null); assert.match(c.CT_API.state.resources.assistant.error, /ai.scopeMismatch/);
  await c.CT_API.readOnce('advisor'); assert.equal(c.CT.advisor.notes.text, 'Portfolio source');
  assert.ok(calls.includes('/advisor'));
});

function labOffice() {
  const frames = [], timers = [], sources = [], listeners = {};
  const counts = { fills: 0, images: 0 };
  const pen = () => ({ imageSmoothingEnabled: true, filter: 'none', fillStyle: '',
    fillRect() { counts.fills++; }, drawImage() { counts.images++; },
    save() {}, restore() {}, translate() {}, scale() {} });
  const canvas = () => ({ width: 0, height: 0, getContext: pen });
  const bubbles = { children: 0, replaceChildren() { this.children = 0; }, appendChild() { this.children++; } };
  const document = { hidden: false,
    currentScript: { src: 'https://console.test/ui/assets/lab/pixel-office.js?v=abc123' },
    querySelector: selector => selector === '#pixel-office' ? canvas()
      : selector === '#ambient-bubbles' ? bubbles : null,
    createElement: tag => tag === 'canvas' ? canvas() : { style: {}, className: '', textContent: '' },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); } };
  const ctx = vm.createContext({ document, URL, console,
    Image: class { set src(value) { sources.push(value); setImmediate(() => this.onload()); } },
    requestAnimationFrame: fn => frames.push(fn), setTimeout: fn => timers.push(fn), clearTimeout() {} });
  ctx.window = ctx;
  vm.runInContext(fs.readFileSync(path.join(root, 'ui/assets/lab/pixel-office.js'), 'utf8'), ctx);
  ctx.createLabPixelScene(document);
  const roles = Array.from({ length: 12 }, (_, i) => ({ id: 'r' + i, name: 'R' + i, state: 'idle' }));
  return { ctx, document, frames, timers, sources, listeners, counts, roles, scene: () => ctx.labPixelScene };
}

test('the Lab office draws only while it is on screen, and slowly at rest', async () => {
  const lab = labOffice(); await flush();
  assert.ok(lab.sources.length > 10 && lab.sources.every(src => src.endsWith('?v=abc123')),
    'sprites carry the release stamp, so they are kept rather than asked for again');
  assert.equal(lab.frames.length, 0, 'at rest the office waits a tenth of a second between frames');
  assert.equal(lab.timers.length, 1);
  lab.timers.shift()();
  assert.equal(lab.frames.length, 1);
  const fills = lab.counts.fills;
  lab.frames.shift()(1000);
  assert.ok(lab.counts.fills - fills <= 1, 'the floor and furniture are drawn once, not every frame');
  assert.equal(lab.timers.length, 1, 'and it keeps going while it is on screen');

  lab.document.hidden = true;                  // another page, or the app in the background
  lab.timers.shift()(); lab.frames.shift()(1100);
  assert.deepEqual([lab.frames.length, lab.timers.length], [0, 0], 'off screen nothing is scheduled');
  lab.document.hidden = false; lab.listeners.visibilitychange.forEach(fn => fn());
  assert.equal(lab.timers.length, 1, 'coming back starts it again');

  lab.scene().sync({ roles: lab.roles, selected: '', active: false });
  lab.timers.shift()(); lab.frames.shift()(1200);
  assert.deepEqual([lab.frames.length, lab.timers.length], [0, 0], 'motion off stops the loop');
  lab.scene().sync({ roles: lab.roles, selected: '', active: true });
  assert.equal(lab.timers.length, 1);
  assert.equal(lab.scene().walk('r0', 'r4'), 'started');
  assert.equal(lab.frames.length, 1, 'a walk runs at the display rate from its first step');
});

test('a poll that only moves checked_at does not draw the page again', async () => {
  const c = context(); let draws = 0;
  let health = { status: 'READY', checked_at: new Date(Date.now() - 1000).toISOString() };
  c.fetch = async url => url === '/auth/session'
    ? response({ authenticated: true, csrf_token: 'csrf' }) : response(health);
  c.run('api.js'); c.document.addEventListener('ct:data', () => { draws++; });
  c.CT_API.start(['health']); await flush();
  draws = 0;
  health = { ...health, checked_at: new Date().toISOString() };
  await c.CT_API.readOnce('health');
  assert.equal(draws, 0, 'a kept reading read again is not new data');
  health = { ...health, status: 'PAUSED' };
  await c.CT_API.readOnce('health');
  assert.equal(draws, 1);
});

test('the loading animation and the chrome ask the main thread for nothing per frame', () => {
  const startup = fs.readFileSync(path.join(root, 'ui/assets/startup.css'), 'utf8');
  const bodies = [...startup.matchAll(/@keyframes\s+[\w-]+\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}/g)].map(m => m[1]);
  assert.ok(bodies.length >= 5);
  for (const body of bodies) {
    const moved = new Set([...body.matchAll(/([a-z-]+)\s*:/g)].map(m => m[1]));
    assert.deepEqual([...moved].filter(name => !['transform', 'opacity'].includes(name)), [], body);
  }
  /* The Lab's status card sits over nothing that moves: a blur there is recomputed on every
     scrolled frame and shows nothing (f44292c). Only the controls floating over the office,
     where there is something to blur, blur. */
  const lab = fs.readFileSync(path.join(root, 'ui/assets/lab.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const now = [...lab.matchAll(/\.lab-now\s*\{[^}]*\}/g)].map(m => m[0]);
  assert.ok(now.length > 0, 'the status card is styled');
  now.forEach(rule => assert.doesNotMatch(rule, /backdrop-filter/, rule));
  assert.deepEqual([...lab.matchAll(/([^{}]+)\{[^}]*backdrop-filter:\s*blur/g)].map(m => m[1].trim()), ['.lab-float']);
  const chrome = fs.readFileSync(path.join(root, 'ui/assets/console.css'), 'utf8');
  assert.match(chrome, /\.topnav \{ background: #fff;[^}]*backdrop-filter: none;/);
  assert.match(chrome, /html:has\(dialog\[open\]\) \.assistant-launcher::before \{ animation-play-state: paused; \}/);
});
test('several data events in one task draw the page once, before the next frame', async () => {
  const c = context(); let draws = 0;
  c.CT_NAV = { bind() {} };
  c.element('blank-state').remove = function () { this.removed = true; };
  c.fetch = async url => response(url === '/auth/session' ? { authenticated: true, csrf_token: 'csrf' }
    : url === '/proposals' ? [] : url === '/health' ? { status: 'READY' } : { accounts: {}, buckets: {} });
  c.run('api.js'); c.run('console.js');
  c.Console.live(['portfolio'], () => { draws++; });
  await flush();
  const before = draws;
  assert.ok(before > 0);
  for (let i = 0; i < 3; i++) c.document.dispatchEvent({ type: 'ct:data' });
  assert.equal(draws, before, 'drawn after the task, not once per event inside it');
  await flush();
  assert.equal(draws, before + 1);
});

test('an unchanged connection tick writes nothing to the page', async () => {
  const c = context();
  c.fetch = async url => response(url === '/auth/session' ? { authenticated: true, csrf_token: 'csrf' } : []);
  c.run('api.js'); c.run('console.js');
  c.CT_API.start(['proposals']); await flush();
  const bar = c.element('conn-bar'); let writes = 0;
  for (const name of ['className', 'hidden', 'textContent', 'innerHTML']) {
    let value = bar[name];
    Object.defineProperty(bar, name, { get: () => value, set: next => { writes++; value = next; } });
  }
  c.Console.paintConnection();
  const first = writes;
  assert.equal(bar.hidden, true, 'every source is current, so the bar is out of the way');
  c.Console.paintConnection(); c.Console.paintConnection();
  assert.equal(writes, first, 'the one-second tick repaints only what changed');
});
