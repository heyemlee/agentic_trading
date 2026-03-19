window.Console.page('advisor', function (document) {
  'use strict';
  const C = window.Console, A = window.CT_API, D = C.D;
  const el = id => document.getElementById(id), h = C.escapeHtml, t = C.t;
  let binding = { scope: 'personal', account: '' }, accountChoice = 'personal:';
  let scopeEpoch = 0, conversationBlocked = false, requestedConversation = null;
  const bindingKey = () => binding.scope + ':' + (binding.account || 'all');
  const currentState = () => A.matchesAssistant(D.assistant) && D.assistant.scope === binding.scope &&
    (D.assistant.account_id || '') === (binding.scope === 'agentic' ? 'micro_live' : binding.account) ? D.assistant : null;
  const knownAccount = () => !binding.account || (D.accounts?.accounts || []).some(row => row.scope === 'personal' && row.account_id === binding.account);
  const available = () => A.writesEnabled && A.isLive('assistant') && !!currentState() && knownAccount();
  const scopeLink = (page = 'advisor', extra = {}) => {
    const q = new URLSearchParams({ scope: binding.scope });
    if (binding.account) q.set('account', binding.account);
    Object.entries(extra).forEach(([key, value]) => { if (value) q.set(key, value); });
    if (window.CT_DEMO) q.set('demo', '1');
    return page + '.html?' + q;
  };
  const matchesConversation = data => !data.scope && Array.isArray(data.messages) && !data.messages.length || data.scope === binding.scope &&
    (binding.scope === 'agentic' ? data.account_scope_kind === 'single' : binding.account
      ? data.account_scope_kind === 'single' && data.account_id === binding.account
      : ['all_personal', 'unknown'].includes(data.account_scope_kind));
  let conversation = 'default', conversationSignature = '', conversationLoading = false;
  let conversationEpoch = 0;
  let notesRevision = 0, notesLoaded = false, notesDirty = false;
  let busy = false, pendingRequest = null, shownMessages = [];
  let session = null, storageOK = false;
  let chatWorking = false, queuedChat = null;
  let followingLatest = true;
  let composerValue = null, composerWidth = null, viewportFrame = null;
  const viewportTimers = new Set();
  const entryFor = id => session && (session.state.entries[id] ||= { draft: '', title: '' });
  function resizeComposer() {
    const input = el('chat-input');
    // Repeated auto-height writes while iOS keeps the caret visible can pan the
    // document. Polls and viewport scrolls do not change the draft's height.
    if (input.value === composerValue && input.clientWidth === composerWidth) return;
    composerValue = input.value; composerWidth = input.clientWidth;
    const scroll = input.scrollTop;
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight || 44, 132) + 'px';
    input.scrollTop = scroll;
  }
  function updateSend() {
    el('chat-send').disabled = busy || chatWorking ||
      conversationBlocked || !available() || !el('chat-input').value.trim();
  }
  function updateLatest() {
    const log = el('chat-scroll');
    el('chat-latest').hidden = !shownMessages.length || log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  }
  function followLatest() {
    const log = el('chat-scroll');
    if (followingLatest && shownMessages.length) log.scrollTop = log.scrollHeight;
    updateLatest();
  }
  function jumpToLatest() {
    followingLatest = true;
    followLatest();
  }
  function remember() {
    if (!session) return false;
    session.state.selected = conversation;
    entryFor(conversation).draft = el('chat-input').value;
    session.state.note = notesDirty ? { text: el('personal-notes').value, revision: notesRevision } : null;
    storageOK = session.save();
    return storageOK;
  }
  function restoreSession() {
    if (session || window.CT_DEMO || !A.authenticated) return;
    session = window.AdvisorSession.open(A.username, bindingKey());
    conversation = session.state.selected;
    if (requestedConversation && session.validId(requestedConversation)) conversation = requestedConversation;
    requestedConversation = null;
    el('chat-input').value = entryFor(conversation).draft;
    pendingRequest = entryFor(conversation).pending || null;
    if (binding.scope === 'personal' && session.state.note) {
      el('personal-notes').value = session.state.note.text;
      notesRevision = session.state.note.revision;
      notesDirty = true; notesLoaded = true;
      el('notes-save-status').textContent = t('ux.draftRestored');
    }
    if (el('chat-input').value) status(t('ux.draftRestored'));
    remember();
  }
  function chooseConversation(id) {
    remember();
    status('');
    conversation = id; shownMessages = []; conversationSignature = '';
    followingLatest = true;
    conversationEpoch++; conversationLoading = false; conversationBlocked = false;
    el('chat-input').value = entryFor(id)?.draft || '';
    pendingRequest = entryFor(id)?.pending || null;
    el('chat-scroll').scrollTop = 0;
    remember(); paintMessages(); render();
  }
  const painted = new Map();
  const paint = (id, html) => { if (painted.get(id) !== html) { el(id).innerHTML = html; painted.set(id, html); } };
  let decisionDetails = new Map();
  let shownDecision = null, shownDecisionJob = null, decisionReadAttempted = false;
  async function markDecisionRead() {
    const runId = shownDecision, job = shownDecisionJob;
    if (!runId || decisionReadAttempted || job?.run_key !== runId || job.state !== 'COMPLETE' || job.opened_at ||
        document.hidden || !el('decision-sheet').open || el('decision-sheet').classList.contains('is-closing') || !A.writesEnabled || window.CT_DEMO) return;
    decisionReadAttempted = true;
    try { await A.markOpened(runId); }
    catch (error) {
      if (shownDecision === runId && !document.hidden) status(t('rs.openedFailed', { err: error.message }));
    }
  }
  C.mount('advisor');

  /* Monoline SVG, never a typographic character: ◎ ◷ ＋ come from three
     different fonts, so their weight, size and baseline never agreed. */
  document.querySelectorAll('[data-icon]').forEach((el) => {
    el.innerHTML = C.ICON[el.dataset.icon] || '';
  });
  /* Discover's search and filters stay under the header while the list scrolls. They
     turn to glass only once rows pass beneath them, where the blur has something to show. */
  let toolbarWatch = null;
  function watchToolbar() {
    if (!window.IntersectionObserver || !window.getComputedStyle) return;
    const bar = el('dc-toolbar'), edge = parseFloat(getComputedStyle(bar).top) || 0;
    toolbarWatch?.disconnect();
    toolbarWatch = new IntersectionObserver(([entry]) => {
      // A hidden panel reports an empty box; leave the bar as it was.
      if (entry.boundingClientRect.width) bar.classList.toggle('is-stuck', !entry.isIntersecting && entry.boundingClientRect.top < edge);
    }, { rootMargin: -Math.ceil(edge) + 'px 0px 0px 0px' });
    toolbarWatch.observe(el('dc-sentinel'));
  }
  const views = ['chat', 'discover'];
  function showView(view, focus = false) {
    if (binding.scope === 'agentic') view = 'chat';
    if (!views.includes(view)) view = 'chat';
    // The chat shell does not scroll as a document. Clear the previous page's
    // position before entering it so WebKit cannot restore that position later.
    if (view === 'chat') window.scrollTo?.(0, 0);
    document.body?.classList.toggle('chat-view', view === 'chat');
    for (const name of views) {
      el('panel-' + name).hidden = name !== view;
      el('view-' + name).setAttribute('aria-selected', String(name === view));
      el('view-' + name).tabIndex = name === view ? 0 : -1;
    }
    // The lens slides under the chosen view; history and a new conversation belong to Chat.
    el('view-switch').dataset.index = String(views.indexOf(view));
    el('chat-tools').hidden = view !== 'chat';
    el('panel-personal').hidden = view !== 'chat' || binding.scope !== 'personal';
    el('panel-tools').hidden = view === 'chat' || binding.scope !== 'personal';
    if (focus) el('view-' + view).focus();
    if (view === 'chat') scheduleViewport();
    else { clearViewport(); watchToolbar(); }
  }
  for (const [index, name] of views.entries()) {
    el('view-' + name).addEventListener('click', () => showView(name));
    el('view-' + name).addEventListener('keydown', e => {
      const next = e.key === 'ArrowRight' ? (index + 1) % views.length : e.key === 'ArrowLeft' ? (index + views.length - 1) % views.length
        : e.key === 'Home' ? 0 : e.key === 'End' ? views.length - 1 : null;
      if (next !== null) { e.preventDefault(); showView(views[next], true); }
    });
  }
  const params = new URLSearchParams(location.search);
  if (params.get('view') === 'holdings') location.replace('portfolio.html' + (window.CT_DEMO ? '?demo=1' : ''));
  showView(params.get('view'));
  function fitViewport() {
    viewportFrame = null;
    if (document.hidden || !document.body?.classList.contains('chat-view')) return;
    const viewport = window.visualViewport;
    // Pinch zoom must still pan naturally; it is not a software keyboard.
    if (viewport && viewport.scale > 1.05) return;
    const height = viewport?.height || window.innerHeight;
    if (!(height > 0)) return;
    const layoutHeight = Math.max(window.innerHeight, document.documentElement.clientHeight);
    // Browser chrome and route restoration can shorten the visual viewport
    // without a keyboard. Only an active editor may switch to keyboard mode.
    const editor = document.activeElement?.matches('input, textarea, select, [contenteditable]');
    const keyboard = !!editor && layoutHeight - height > 140;
    // iOS may pan the visual viewport to the caret without a resize. Once the
    // keyboard closes, ignore a stale offsetTop instead of leaving a blank gap.
    const top = keyboard ? Math.max(0, viewport?.offsetTop || 0) : 0;
    // After dismissal WebKit can retain a shortened visual viewport. Restore
    // the CSS full-screen height (stable vh in installed apps); only an open
    // keyboard needs a measured pixel height.
    if (keyboard) document.body.style.setProperty('--app-height', height + 'px');
    else document.body.style.removeProperty('--app-height');
    document.body.style.setProperty('--chat-top', top + 'px');
    document.body.classList.toggle('assistant-keyboard', keyboard);
    // WebKit may leave the layout viewport scrolled after hiding the keyboard,
    // including when the textarea stays focused. Reset only the document; the
    // conversation and textarea retain their own independent reading positions.
    if (!keyboard && (window.scrollX || window.scrollY || viewport?.offsetTop)) window.scrollTo?.(0, 0);
    resizeComposer();
    followLatest();
  }
  function scheduleViewport() {
    if (!document.body || document.hidden || viewportFrame !== null) return;
    viewportFrame = window.requestAnimationFrame(fitViewport);
  }
  function settleViewport() {
    scheduleViewport();
    // Coalesce animation events, then measure again after WebKit settles.
    viewportTimers.forEach(clearTimeout); viewportTimers.clear();
    for (const delay of [250, 700]) {
      const timer = setTimeout(() => { viewportTimers.delete(timer); scheduleViewport(); }, delay);
      viewportTimers.add(timer);
    }
  }
  window.visualViewport?.addEventListener('resize', settleViewport);
  window.visualViewport?.addEventListener('scroll', scheduleViewport);
  window.addEventListener?.('scroll', scheduleViewport);
  window.addEventListener?.('resize', settleViewport);
  window.addEventListener?.('pageshow', settleViewport);
  document.addEventListener('visibilitychange', settleViewport);
  document.addEventListener('focusin', settleViewport);
  document.addEventListener('focusout', settleViewport);
  function clearViewport() {
    if (viewportFrame !== null) window.cancelAnimationFrame(viewportFrame);
    viewportFrame = null;
    viewportTimers.forEach(clearTimeout); viewportTimers.clear();
    document.body?.style.removeProperty('--app-height');
    document.body?.style.removeProperty('--chat-top');
    document.body?.classList.remove('assistant-keyboard');
  }
  document.addEventListener('ct:leave', clearViewport);
  const status = text => { el('advisor-status').textContent = text; };
  const setText = (id, text) => { if (el(id).textContent !== text) el(id).textContent = text; };
  // Chat's status line sits in its composer, out of sight on Discover; this one is Discover's own.
  const discoverSay = text => setText('discover-status', text);
  const active = j => j.state === 'RUNNING' || j.state === 'QUEUED';
  /* The account the assistant sees. One tap on a row switches: each row links to this page for
     that scope, so the router pushes it as history that the header's Back skips (navigation.js,
     ctAssistantReturnDepth), and routeEntry rebinds the drafts, conversation and reads to it. */
  const ACCOUNT_ICON = {
    all: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5 3.5 8 12 12.5 20.5 8z"/><path d="M3.5 12.5 12 17l8.5-4.5M3.5 16.5 12 21l8.5-4.5"/></svg>',
    one: C.ICON.more,
    agentic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 3 5.5 13.5H11L10.5 21 18.5 10.5H13z"/></svg>',
  };
  const choiceLink = key => {
    const q = new URLSearchParams({ scope: key === 'agentic' ? 'agentic' : 'personal' });
    if (key.startsWith('personal:') && key.slice(9)) q.set('account', key.slice(9));
    if (window.CT_DEMO) q.set('demo', '1');
    return 'advisor.html?' + q;
  };
  const choiceRow = ([key, label, hint, kind]) => '<a class="account-choice is-' + kind + '" data-account-choice="' + h(key) +
    '" href="' + h(choiceLink(key)) + '"' + (key === accountChoice ? ' aria-current="true"' : '') + '><span class="account-orb" aria-hidden="true">' +
    ACCOUNT_ICON[kind] + '</span><span class="account-copy"><strong>' + h(label) + '</strong><span>' + h(hint) + '</span></span>' +
    '<span class="account-check" aria-hidden="true">' + (key === accountChoice ? C.ICON.check : '') + '</span></a>';
  el('assistant-account').addEventListener('click', e => {
    /* Another account closes every sheet at once as routeEntry rebinds. Only the current one is
       closed here: an animated close started for a switch would still be pending when the
       picker is opened again, and its timer would shut the new one (console.js, closeSheet). */
    if (e.target.closest?.('[data-account-choice]')?.dataset.accountChoice === accountChoice) C.closeSheet('account-sheet');
  });
  el('context-account').addEventListener('click', () => {
    // From the tools sheet, the picker takes its place and hands focus back to the header.
    el('context-sheet').close();
    el('assistant-context').focus?.();
    C.openSheet('account-sheet');
  });
  function renderScope(state) {
    const personal = [['personal:', t('ai.allPersonal'), t('ai.pickAll'), 'all'],
      ...(D.accounts?.accounts || []).filter(row => row.scope === 'personal' && row.account_scope_kind === 'single')
        .map(row => ['personal:' + row.account_id, row.account_label || row.account_id, t('ai.pickOne'), 'one'])];
    // A binding the directory does not know is still listed, as the current one, never dropped.
    if (accountChoice !== 'agentic' && !personal.some(([key]) => key === accountChoice))
      personal.push([accountChoice, binding.account || t('ai.scopeMismatch'), t('ai.pickOne'), 'one']);
    paint('assistant-account', '<p class="account-group">' + h(t('ai.groupPersonal')) + '</p>' + personal.map(choiceRow).join('') +
      '<p class="account-group">' + h(t('ai.groupManaged')) + '</p>' + choiceRow(['agentic', 'Agentic', t('ai.pickAgentic'), 'agentic']));
    // Until this account's own reading arrives, its name comes from the directory, never its id.
    const named = (D.accounts?.accounts || []).find(row => row.scope === 'personal' && row.account_id === binding.account)?.account_label;
    const label = state.account_label || (binding.account ? named || binding.account : binding.scope === 'agentic' ? 'Agentic' : t('ai.allPersonal'));
    setText('assistant-account-label', label);
    setText('context-account-name', label);
    el('assistant-context').setAttribute('aria-label', t('ai.accountButton', { label }));
    el('assistant-context-summary').textContent = state.snapshot?.fetched_at
      ? t('ai.fresh', { date: C.dateTime(state.snapshot.fetched_at) }) : t('ai.contextHint');
    setText('assistant-scope-note', t(binding.scope === 'agentic' ? 'ai.agenticOnly' : 'ai.personalOnly'));
    /* What is wrong with the account's reading, said in the picker and signalled on the capsule
       by its dot: ink when current, hollow while loading, amber when there is something to read.
       The demo's fixtures are its reading, so nothing there goes out of date. */
    const source = A.state?.resources?.assistant, live = key => window.CT_DEMO || A.isLive(key);
    const [note, tone] = !['personal', 'agentic'].includes(binding.scope) ? [t('ai.scopeMismatch'), 'warn']
      : !knownAccount() && D.accounts ? [t('ai.unknownAccount'), 'warn']
      : !currentState() ? (source?.error ? [source.error, 'warn'] : [t('common.loading'), 'loading'])
      : !live('assistant') ? [t('ai.scopeStale'), 'warn']
      : !live('accounts') || D.accounts?.personal_snapshot?.stale && binding.scope === 'personal' ? [t('rs.accountsPartial'), 'warn'] : ['', ''];
    setText('assistant-account-status', note);
    el('assistant-account-status').className = 'account-status' + (tone ? ' is-' + tone : '');
    el('scope-dot').className = 'scope-dot' + (tone ? ' is-' + tone : '');
    el('assistant-coverage').textContent = state.coverage_info?.note || state.coverage || '';
    el('assistant-research-link').href = scopeLink('research');
    el('assistant-chat-note').textContent = t(binding.scope === 'agentic' ? 'ai.agenticOnly' : 'ai.scopedChatNote');
    // Discover is personal only; with one view left, the switch has nothing to switch.
    el('view-discover').hidden = binding.scope !== 'personal';
    el('assistant-subhead').hidden = binding.scope !== 'personal';
    el('agentic-records-panel').hidden = binding.scope !== 'agentic';
    document.querySelectorAll('[data-open-sheet="personal-sheet"]').forEach(button => { button.hidden = binding.scope !== 'personal'; });
    el('panel-personal').hidden = binding.scope !== 'personal' || el('panel-chat').hidden;
  }
  /* Discover lists the candidate pool from the last snapshot. A source reads
     "watchlists:<list>", "scans:<screener>" or "profile:<group>" (advisor.fetch_snapshot);
     the list or group name is the owner's own text and renders verbatim. A row starts
     research only from its own button: a full review spends the day's research budget. */
  const SOURCE_KINDS = ['watchlists', 'scans', 'profile'];
  const CANDIDATE_PAGE = 24;
  const SYMBOL = /^[A-Z][A-Z0-9.-]{0,19}$/;      // the service's own rule, advisor.SYMBOL
  let candidateFilter = 'all', candidateQuery = '', candidateLimit = CANDIDATE_PAGE;
  const sourceKind = source => {
    const kind = String(source).split(':')[0];
    return SOURCE_KINDS.includes(kind) ? kind : 'other';
  };
  const sourceName = source => String(source).slice(String(source).indexOf(':') + 1);
  const isDay = day => /^\d{4}-\d{2}-\d{2}$/.test(day || '');
  const shortDate = day => isDay(day)
    ? new Date(day + 'T00:00:00Z').toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' }) : '';
  const matchRank = symbol => symbol === candidateQuery ? 2 : symbol.startsWith(candidateQuery) ? 1 : 0;
  function candidateRow(c, held, running, latest) {
    const sources = (c.sources || []).map(source => sourceKind(source) === 'other' ? '<span>' + h(source) + '</span>'
      : '<span><b>' + h(t('dc.kind.' + sourceKind(source))) + '</b> ' + h(sourceName(source)) + '</span>').join('<i aria-hidden="true"> · </i>');
    const last = latest.get(c.symbol);
    const tags = (held.has(c.symbol) ? '<span class="dc-tag is-held">' + h(t('dc.held')) + '</span>' : '') +
      (last ? '<span class="dc-tag">' + h(t('dc.last', { action: C.code(last.action), date: shortDate(last.trade_date) || '—' })) + '</span>' : '');
    const action = running.has(c.symbol)
      ? '<span class="dc-running"><span class="dc-pulse" aria-hidden="true"></span>' + h(t('dc.researching')) + '</span>'
      : '<button type="button" class="dc-go" data-research="' + h(c.symbol) + '" aria-label="' + h(t('dc.researchSymbol', { symbol: c.symbol })) + '">' + h(t('ux.review')) + '</button>';
    return '<div class="dc-row' + (held.has(c.symbol) ? ' is-held' : '') + '" role="listitem"><span class="dc-mono" aria-hidden="true">' + h(c.symbol.slice(0, 1)) +
      '</span><div class="dc-main"><p class="dc-line"><strong class="dc-symbol">' + h(c.symbol) + '</strong>' + tags + '</p><p class="dc-sources">' + sources + '</p></div>' + action + '</div>';
  }
  // Ranking an empty pool would still spend one of the day's assistant replies.
  const shortlistOff = () => busy || chatWorking || conversationBlocked || binding.scope !== 'personal' || !available() ||
    !(currentState()?.snapshot?.candidates || []).length;
  function syncResearchButtons() {
    const off = busy || binding.scope !== 'personal' || !available();
    document.querySelectorAll('[data-research]').forEach(button => { button.disabled = off; });
    document.querySelectorAll('#symbol-form button').forEach(button => { button.disabled = off || !el('symbol-input').value.trim(); });
  }
  function renderDiscover(state) {
    const snapshot = state.snapshot || {}, candidates = snapshot.candidates || [], loaded = !!currentState();
    const counts = { all: candidates.length };
    candidates.forEach(c => new Set((c.sources || []).map(sourceKind)).forEach(kind => { counts[kind] = (counts[kind] || 0) + 1; }));
    const kinds = ['all', ...SOURCE_KINDS, 'other'].filter(kind => kind === 'all' || counts[kind]);
    if (!kinds.includes(candidateFilter)) candidateFilter = 'all';
    paint('dc-pool', !loaded ? '' : !candidates.length ? '<span>' + h(t('dc.poolEmpty')) + '</span>'
      : '<span>' + h(t('dc.pool', { n: candidates.length })) + '</span>' +
        (snapshot.fetched_at ? '<span>' + h(t('ai.fresh', { date: C.dateTime(snapshot.fetched_at) })) + '</span>' : ''));
    paint('discover-filters', candidates.length ? kinds.map(kind => '<button type="button" class="dc-chip" data-filter="' + kind + '" aria-pressed="' +
      (kind === candidateFilter && !candidateQuery) + '">' + h(t(kind === 'all' ? 'dc.all' : 'dc.kind.' + kind)) +
      '<span class="dc-chip-n">' + counts[kind] + '</span></button>').join('') : '');
    // A search looks through the whole pool; a source filter narrows it only while nothing is typed.
    const shown = candidateQuery
      ? candidates.filter(c => c.symbol.includes(candidateQuery)).sort((a, b) => matchRank(b.symbol) - matchRank(a.symbol))
      : candidateFilter === 'all' ? candidates : candidates.filter(c => (c.sources || []).some(source => sourceKind(source) === candidateFilter));
    const visible = candidateQuery ? shown : shown.slice(0, candidateLimit);
    const held = new Set((snapshot.accounts || []).flatMap(account => (account.positions || []).map(p => p.symbol)));
    const running = new Set((state.jobs || []).filter(j => active(j) && (j.task?.kind || 'analysis') === 'analysis').map(j => j.ticker));
    const latest = new Map();
    (state.decisions || []).forEach(d => { if (d.ticker && !latest.has(d.ticker)) latest.set(d.ticker, d); });
    paint('candidate-list', !loaded ? '<div class="dc-skeleton"></div><div class="dc-skeleton"></div><div class="dc-skeleton"></div>'
      : !candidates.length ? '<p class="dc-empty">' + h(t('dc.noSnapshot')) + '</p>'
        : !shown.length ? '<p class="dc-empty">' + h(candidateQuery ? t('dc.noMatch', { q: candidateQuery }) : t('dc.noneInFilter')) + '</p>'
          : visible.map(c => candidateRow(c, held, running, latest)).join(''));
    const rest = shown.length - visible.length;
    el('candidate-more').hidden = rest <= 0;
    setText('candidate-more', rest > 0 ? t('dc.more', { n: Math.min(rest, CANDIDATE_PAGE * 2) }) : '');
    setText('dc-count', loaded && candidates.length ? String(shown.length) : '');
    const gaps = snapshot.errors || [];
    el('dc-gaps').hidden = !gaps.length;
    setText('dc-gaps-title', gaps.length ? t('dc.gaps', { n: gaps.length }) : '');
    paint('dc-gaps-list', gaps.map(gap => '<li>' + h(gap) + '</li>').join(''));
    syncResearchButtons();
  }
  function researchButton(symbol, account) {
    if (binding.scope === 'agentic') return '<a class="btn btn-secondary btn-sm" href="' + h(scopeLink('research', { ticker: symbol })) + '">' + t('ai.research') + '</a>';
    return '<button type="button" class="btn btn-secondary btn-sm" data-research="' + h(symbol) + '"' +
      (account ? ' data-account="' + h(account) + '"' : '') + '>' + t('ai.research') + '</button>';
  }
  function paintMessages() {
    paint('chat-messages', shownMessages.length ? shownMessages.map((m, index) =>
      '<div class="chat-message ' + (m.role === 'user' ? 'user' : 'assistant') + '">' +
      '<p class="eyebrow">' + t(m.role === 'user' ? 'ai.you' : 'ai.assistant') + '</p>' +
      '<div class="md">' + C.md(m.content) + '</div>' +
      (m.research_candidates || []).map(c => '<div class="advisor-row"><div><strong>' + h(c.symbol) +
        '</strong><p class="body-sm">' + h(c.reason) + '</p></div>' + researchButton(c.symbol) + '</div>').join('') +
      (m.role !== 'user' && m.content ? '<button class="message-copy" data-copy-message="' + index + '">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/></svg>' + t('ai.copy') + '</button>' : '') +
      (m.role === 'user' && m.state !== 'COMPLETE' ? '<p class="body-sm">' + h(C.code(m.state)) + '</p>' : '') + '</div>'
    ).join('') : '');
    el('chat-welcome').hidden = shownMessages.length > 0 || chatWorking;
    followLatest();
  }
  async function loadConversation(signature) {
    if (conversationLoading === conversation || window.CT_DEMO) return;
    const selected = conversation;
    const epoch = ++conversationEpoch;
    conversationLoading = selected;
    try {
      const data = await A.fetchJSON('/advisor/conversations/' + encodeURIComponent(selected));
      if (selected !== conversation || epoch !== conversationEpoch) return;
      if (data.conversation_id !== selected || !matchesConversation(data)) {
        conversationBlocked = true; conversationSignature = signature; shownMessages = []; paintMessages(); status(t('ai.conversationMismatch')); return;
      }
      conversationBlocked = false;
      shownMessages = data.messages || [];
      const firstQuestion = shownMessages.find(m => m.role === 'user' && m.content?.trim());
      const entry = entryFor(selected);
      if (entry && !entry.title && firstQuestion) {
        entry.title = firstQuestion.content.trim().slice(0, 90); remember();
      }
      conversationSignature = signature;
      paintMessages();
    } catch (err) { if (epoch === conversationEpoch) { conversationBlocked = true; status(t('ai.error', { error: err.message })); } }
    finally { if (epoch === conversationEpoch) { conversationLoading = false; updateSend(); el('shortlist').disabled = shortlistOff(); } }
  }
  function render() {
    restoreSession();
    const state = currentState() || {}, snapshot = state.snapshot || {}, jobs = state.jobs || [];
    renderScope(state);
    const decisions = state.decisions || [];
    renderDiscover(state);
    // Newest completed analysis per account/symbol. Historical conditions always carry a date.
    const seen = new Set();
    decisionDetails = new Map();
    const decisionCards = decisions.filter(d => {
      const key = (d.account_id || d.bucket) + ':' + d.ticker;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 12).map(d => {
      const p = d.policy?.proposal || {};
      const detail = (key, value) => value ? '<dt>' + t(key) + '</dt><dd>' + h(value) + '</dd>' : '';
      const scope = d.account_id
        ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket);
      decisionDetails.set(d.run_id, '<div class="research-sheet-hero"><div><h3>' + h(d.ticker) + '</h3><p>' + h(C.code(d.action)) + ' · ' + h(d.trade_date) + '</p></div></div><p class="body-sm">' + h(d.account_id
          ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket)) +
        '</p><p>' + h(p.investment_thesis || '') + '</p><dl>' + detail('ai.entry', p.entry_logic) +
        detail('ai.exit', p.exit_logic) + detail('ai.risks', [...(p.primary_risks || []), ...(p.missing_information || [])].join('\n')) +
        '</dl><div class="advisor-actions"><a class="btn btn-secondary btn-sm" href="' + h(scopeLink('research', { run: d.run_id, account: binding.scope === 'personal' && d.account_scope_kind !== 'all_personal' ? d.account_id : '', include_all_personal: d.account_scope_kind === 'all_personal' ? 'true' : '' })) +
        '">' + t('ai.fullReport') + '</a>' + researchButton(d.ticker, d.account_id) + '</div>');
      // Ink for an entry, an outline for an exit, grey for standing still: the word says which.
      const tone = ['BUY', 'ADD'].includes(d.action) ? ' is-entry' : ['REDUCE', 'SELL'].includes(d.action) ? ' is-exit' : '';
      return '<button class="research-tile" data-decision="' + h(d.run_id) + '" aria-haspopup="dialog"><span class="rt-symbol">' + h(d.ticker) +
        '</span><span class="action-badge' + tone + '">' + h(C.code(d.action)) + '</span><span class="rt-meta">' + h([scope, isDay(d.trade_date) ? C.dateOnly(d.trade_date) : d.trade_date].filter(Boolean).join(' · ')) +
        '</span>' + (p.investment_thesis ? '<span class="rt-thesis">' + h(p.investment_thesis) + '</span>' : '') + '</button>';
    }).join('');
    paint('holding-decisions', decisionCards); paint('agentic-records', decisionCards);
    el('dc-recent').hidden = !decisionCards;
    const activeJobs = jobs.filter(active);
    el('jobs-preview').textContent = activeJobs.length ? t('ux.activeJobs', { n: activeJobs.length }) : t('ux.recentJobs', { n: jobs.length });
    el('jobs-count').textContent = String(activeJobs.length || jobs.length);
    paint('advisor-jobs', jobs.slice(0, 8).map(j => {
      const kind = j.task?.kind || 'analysis';
      const title = t('ux.task.' + kind) + (kind === 'analysis' ? ' · ' + j.ticker : '');
      const tone = active(j) ? 'working' : j.state === 'COMPLETE' ? 'done' : 'attention';
      return '<div class="task-row"><span class="task-orb ' + tone + '">' + (tone === 'done' ? C.ICON.check : C.ICON.info) +
        '</span><div class="task-copy"><strong>' + h(title) + '</strong><p>' + h(j.task?.message?.slice(0, 90) || t('ux.taskHint.' + kind)) +
        '</p><div class="task-meta"><span class="task-state ' + tone + '">' + h(C.code(j.state)) + '</span><time>' + h(C.dateTime(j.created_at)) +
        '</time></div>' + (j.error ? '<p class="task-error">' + h(j.error) + '</p>' : '') + '</div>' +
        (active(j) ? '<button class="btn btn-secondary btn-sm" data-cancel="' + h(j.run_key) + '">' + t('ai.cancel') + '</button>' : '') + '</div>';
    }).join('') || '<p class="sheet-description">' + t('ux.noTasks') + '</p>');
    if (binding.scope === 'personal' && state.notes && !notesDirty && (!notesLoaded || state.notes.revision !== notesRevision)) {
      el('personal-notes').value = state.notes.text;
      notesRevision = state.notes.revision; notesLoaded = true;
    }
    const summaries = (state.conversation_summaries || []).filter(matchesConversation);
    const choices = [...new Set([conversation, ...summaries.map(row => row.conversation_id), ...Object.keys(session?.state.entries || {})])];
    paint('conversations', choices.map((id, index) => {
      const entry = jobs.filter(j => j.task?.conversation_id === id && j.task.message).at(-1);
      const saved = entryFor(id);
      if (saved && !saved.title && entry?.task.message) saved.title = entry.task.message.slice(0, 90);
      const title = saved?.title || entry?.task.message || (id === 'default' ? t('ai.chat') : t('ux.conversation', { n: index + 1 }));
      return '<button class="conversation-option" data-conversation="' + h(id) + '" aria-pressed="' + (id === conversation) +
        '"><span class="mini-orb">' + C.ICON.info + '</span><span class="utility-copy"><strong>' + h(title.slice(0, 90)) +
        '</strong><span>' + h(entry?.created_at ? C.dateTime(entry.created_at) : t('ai.new')) + '</span></span>' +
        (id === conversation ? '<span class="selected-mark">' + C.ICON.check + '</span>' : '<span class="ux-chevron"></span>') + '</button>';
    }).join(''));
    remember();
    el('notes-preview').textContent = binding.scope === 'personal' ? state.notes?.text || t('ux.personalHint') : '';
    const chatting = jobs.some(j => active(j) && j.task?.conversation_id === conversation);
    if (queuedChat?.conversation === conversation) {
      const finished = jobs.find(j => j.run_key === queuedChat.run);
      if (finished && !active(finished)) {
        status(finished.error ? t('ai.error', { error: finished.error }) : '');
        queuedChat = null;
      }
    }
    chatWorking = chatting;
    el('chat-progress').hidden = !chatting;
    el('chat-welcome').hidden = shownMessages.length > 0 || chatting;
    resizeComposer(); updateSend();
    el('shortlist').disabled = shortlistOff();
    syncResearchButtons();
    const signature = conversation + JSON.stringify(jobs.filter(j => j.task?.conversation_id === conversation).map(j => [j.run_key, j.state]));
    if (currentState() && signature !== conversationSignature) loadConversation(signature);
    paintMessages();
    if (window.CT_DEMO) { status(t('ai.demo')); discoverSay(t('ai.demo')); }
    updateSend();
  }
  async function send(kind, extra = {}) {
    if (busy || kind === 'chat' && chatWorking || !available() || binding.scope === 'agentic' && kind !== 'chat' ||
        ['chat', 'shortlist'].includes(kind) && conversationBlocked || binding.account && extra.account_id && extra.account_id !== binding.account) return;
    if (['chat', 'shortlist'].includes(kind)) followingLatest = true;
    const fromDiscover = kind === 'analysis' && !el('panel-discover').hidden;
    const report = text => { status(text); if (fromDiscover) discoverSay(text); };
    busy = true; report(t('ai.running')); render();
    const payload = { kind, language: window.I18N.lang, conversation_id: conversation, ...extra,
      scope: binding.scope, account_id: binding.scope === 'agentic' ? null : extra.account_id || binding.account || null };
    const fingerprint = JSON.stringify(payload);
    if (!pendingRequest || pendingRequest.fingerprint !== fingerprint) {
      pendingRequest = { fingerprint, request_id: crypto.randomUUID() };
    }
    const request = pendingRequest, selected = conversation, sentText = el('chat-input').value, sendSession = session;
    if (kind === 'chat' && entryFor(selected)) entryFor(selected).pending = request;
    remember();
    try {
      const result = await A.post('/advisor/jobs', { ...payload, request_id: request.request_id });
      if (session !== sendSession) return;
      if (result.blocked) { report(t('ai.demo')); return; }
      if (pendingRequest === request) pendingRequest = null;
      const saved = entryFor(selected);
      if (kind === 'chat') {
        queuedChat = { conversation: selected, run: result.run_key };
        if (saved?.pending?.request_id === request.request_id) delete saved.pending;
        if (saved && !saved.title) saved.title = (extra.message || '').slice(0, 90);
        if (selected === conversation && el('chat-input').value === sentText) el('chat-input').value = '';
        else if (saved?.draft === sentText) saved.draft = '';
      }
      remember();
      report(kind === 'analysis' ? t('dc.queued', { symbol: payload.ticker }) : t('ai.queued')); conversationSignature = '';
      await A.readOnce('assistant');
      return true;
    } catch (err) { if (session === sendSession) { if (err.status === 409) report(t('ai.conflict') + ' ' + err.message); else report(t('ai.error', { error: err.message })); } }
    finally { busy = false; render(); }
  }
  el('chat-form').addEventListener('submit', e => {
    e.preventDefault(); const message = el('chat-input').value.trim();
    if (message) send('chat', { message });
  });
  el('chat-input').addEventListener('input', () => { resizeComposer(); updateSend(); remember(); });
  el('chat-input').addEventListener('keydown', e => {
    // Phone Enter keeps a newline; desktop Enter sends. Never submit an IME composition.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229 &&
        window.matchMedia?.('(pointer: fine)').matches) {
      e.preventDefault(); el('chat-form').requestSubmit();
    }
  });
  el('chat-scroll').addEventListener('scroll', () => {
    const log = el('chat-scroll');
    // Remember the reader's intent before a render changes message/composer
    // height. Sending explicitly resumes following delayed replies as well.
    followingLatest = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    updateLatest();
  }, { passive: true });
  el('chat-latest').addEventListener('click', jumpToLatest);
  function usePrompt(key) {
    el('chat-input').value = t(key); resizeComposer(); updateSend(); remember();
    el('chat-input').focus();
  }
  el('ask-research').addEventListener('click', () => usePrompt('ai.askResearch'));
  el('ask-scenario').addEventListener('click', () => usePrompt('ai.askScenario'));
  el('shortlist').addEventListener('click', () => { showView('chat'); send('shortlist', { message: t('ai.shortlistPrompt') }); });
  el('symbol-form').addEventListener('submit', async e => {
    e.preventDefault();
    const typed = el('symbol-input').value, ticker = typed.trim().toUpperCase();
    // The service answers a malformed symbol with a generic refusal; say which rule it broke here.
    if (!SYMBOL.test(ticker)) { discoverSay(t('dc.badSymbol')); return; }
    if (await send('analysis', { ticker }) && el('symbol-input').value === typed) {
      el('symbol-input').value = ''; candidateQuery = '';
      renderDiscover(currentState() || {});
    }
  });
  // Typing narrows the list to matching symbols; the button still researches exactly what was typed.
  el('symbol-input').addEventListener('input', () => {
    const query = el('symbol-input').value.trim().toUpperCase();
    if (query === candidateQuery) { syncResearchButtons(); return; }
    candidateQuery = query; candidateLimit = CANDIDATE_PAGE;
    renderDiscover(currentState() || {});
  });
  el('symbol-input').addEventListener('keydown', e => {
    if (e.key !== 'Escape' || !el('symbol-input').value) return;
    el('symbol-input').value = ''; candidateQuery = '';
    renderDiscover(currentState() || {});
  });
  el('discover-filters').addEventListener('click', e => {
    const chip = e.target.closest?.('[data-filter]');
    if (!chip) return;
    candidateFilter = chip.dataset.filter; candidateLimit = CANDIDATE_PAGE;
    if (candidateQuery) { candidateQuery = ''; el('symbol-input').value = ''; }
    renderDiscover(currentState() || {});
  });
  el('candidate-more').addEventListener('click', () => {
    candidateLimit += CANDIDATE_PAGE * 2;
    renderDiscover(currentState() || {});
  });
  el('new-conversation').addEventListener('click', () => {
    chooseConversation(crypto.randomUUID());
  });
  el('personal-notes').addEventListener('input', () => { notesDirty = true; remember(); });
  el('save-notes').addEventListener('click', async () => {
    if (el('save-notes').disabled || binding.scope !== 'personal') return;
    el('save-notes').disabled = true;
    const text = el('personal-notes').value;
    const saveSession = session;
    try {
      const saved = await A.post('/advisor/notes', { text, revision: notesRevision });
      if (session !== saveSession) return;
      if (saved.blocked) { el('notes-save-status').textContent = t('ai.demo'); return; }
      notesRevision = saved.revision;
      notesDirty = el('personal-notes').value !== text;
      remember();
      status(t('ai.saved'));
      el('notes-save-status').textContent = t('ai.saved');
      if (!notesDirty) C.closeSheet('personal-sheet');
      await A.readOnce('assistant');
    } catch (err) { if (session === saveSession) el('notes-save-status').textContent = t('ai.error', { error: err.message }); }
    finally { el('save-notes').disabled = false; }
  });
  document.addEventListener('click', async e => {
    const copy = e.target.closest?.('[data-copy-message]');
    if (copy) {
      const content = shownMessages[Number(copy.dataset.copyMessage)]?.content;
      if (!content) return;
      try { await navigator.clipboard.writeText(content); status(t('ai.copied')); }
      catch { status(t('ai.copyFailed')); }
      return;
    }
    const choice = e.target.closest?.('[data-conversation]');
    if (choice) {
      const remote = (currentState()?.conversation_summaries || []).find(row => row.conversation_id === choice.dataset.conversation);
      if (remote && !matchesConversation(remote)) { status(t('ai.conversationMismatch')); return; }
      C.closeSheet('conversation-sheet'); chooseConversation(choice.dataset.conversation); return;
    }
    const decision = e.target.closest?.('[data-decision]');
    if (decision) {
      const runId = decision.dataset.decision, epoch = scopeEpoch;
      const content = decisionDetails.get(runId);
      if (!content) return;
      shownDecision = runId; shownDecisionJob = null; decisionReadAttempted = false;
      el('decision-detail').innerHTML = content;
      C.openSheet('decision-sheet');
      if (!window.CT_DEMO) {
        try {
          const job = await A.fetchJSON('/research/' + encodeURIComponent(runId));
          if (epoch === scopeEpoch && shownDecision === runId) { shownDecisionJob = job; markDecisionRead(); }
        } catch (_) { /* A historical report can outlive its queue entry; keep it readable. */ }
      }
      return;
    }
    const button = e.target.closest?.('[data-research], [data-cancel]');
    if (!button || button.disabled) return;
    if (button.dataset.research) {
      if (el('decision-sheet').open) C.closeSheet('decision-sheet');
      await send('analysis', { ticker: button.dataset.research, account_id: button.dataset.account || null });
    } else {
      button.disabled = true;
      try {
        const result = await A.post('/research/' + encodeURIComponent(button.dataset.cancel) + '/cancel', {});
        if (result.blocked) { status(t('ai.demo')); return; }
        await A.readOnce('assistant');
      } catch (err) { status(t('ai.error', { error: err.message })); }
      finally { button.disabled = false; }
    }
  });
  document.addEventListener('ct:lang', paintMessages);
  document.addEventListener('visibilitychange', markDecisionRead);
  document.addEventListener('ct:leave', () => { shownDecision = null; shownDecisionJob = null; });
  document.addEventListener('ct:auth', e => {
    if (e.detail?.clearDrafts || (e.detail?.authenticated && session && session.state.owner !== e.detail.username)) {
      shownDecision = null; shownDecisionJob = null;
      session?.clear(); session = null; notesDirty = false; notesLoaded = false;
      conversation = 'default'; pendingRequest = null; shownMessages = []; conversationSignature = ''; conversationBlocked = false;
      followingLatest = true;
      conversationLoading = false; conversationEpoch++;
      el('chat-input').value = ''; el('personal-notes').value = ''; paintMessages();
    }
  });
  window.addEventListener?.('beforeunload', e => {
    remember();
    const hasDraft = notesDirty || Object.values(session?.state.entries || {}).some(x => x.draft) || el('chat-input').value;
    if (busy || (hasDraft && !storageOK)) { e.preventDefault(); e.returnValue = ''; }
  });
  window.addEventListener?.('pagehide', remember);
  /* The personal context has its own page now. Borrowing this one as a
     container left people on the Assistant after they closed the sheet, so
     the deep link is gone; the sheet stays for editing mid-conversation. */
  if (params.get('sheet') === 'jobs') C.openSheet('jobs-sheet');
  function routeEntry() {
    settleViewport();
    const next = new URLSearchParams(location.search);
    const selected = { scope: next.get('scope') || 'personal', account: next.get('account') || '' };
    if (selected.scope === 'agentic') selected.account = '';
    const changed = JSON.stringify(selected) !== JSON.stringify(binding);
    if (changed) {
      remember(); binding = selected; scopeEpoch++; conversationEpoch++; session = null;
      conversation = 'default'; conversationLoading = false; conversationSignature = ''; conversationBlocked = false;
      shownMessages = []; shownDecision = null; shownDecisionJob = null; pendingRequest = null; queuedChat = null;
      followingLatest = true;
      notesLoaded = false; notesDirty = false; notesRevision = 0;
      el('chat-input').value = ''; el('personal-notes').value = ''; status('');
      candidateFilter = 'all'; candidateQuery = ''; candidateLimit = CANDIDATE_PAGE;
      el('symbol-input').value = ''; discoverSay('');
      ['personal-sheet', 'conversation-sheet', 'decision-sheet', 'jobs-sheet', 'context-sheet', 'account-sheet'].forEach(id => el(id).close());
      document.querySelectorAll('#agentic-records-panel details').forEach(node => { node.open = false; });
      paintMessages();
    }
    accountChoice = binding.scope === 'agentic' ? 'agentic' : 'personal:' + binding.account;
    requestedConversation = next.get('conversation');
    A.selectAssistant(binding);
    restoreSession();
    if (requestedConversation && session?.validId(requestedConversation)) { chooseConversation(requestedConversation); requestedConversation = null; }
    if (changed || next.has('view')) showView(next.get('view'));
    if (next.get('sheet') === 'jobs') C.openSheet('jobs-sheet');
    render();
  }
  document.addEventListener('ct:route', routeEntry);
  routeEntry();
  C.live([], render, ['assistant', 'accounts']);
});
