(function () {
  'use strict';
  const C = window.Console, A = window.CT_API, D = C.D;
  const el = id => document.getElementById(id), h = C.escapeHtml, t = C.t;
  let conversation = 'default', conversationSignature = '', conversationLoading = false;
  let conversationEpoch = 0;
  let notesRevision = 0, notesLoaded = false, notesDirty = false;
  let busy = false, pendingRequest = null, shownMessages = [];
  let session = null, storageOK = false;
  const entryFor = id => session && (session.state.entries[id] ||= { draft: '', title: '' });
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
    session = window.AdvisorSession.open(A.username);
    conversation = session.state.selected;
    el('chat-input').value = entryFor(conversation).draft;
    pendingRequest = entryFor(conversation).pending || null;
    if (session.state.note) {
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
    conversation = id; shownMessages = []; conversationSignature = '';
    el('chat-input').value = entryFor(id)?.draft || '';
    pendingRequest = entryFor(id)?.pending || null;
    remember(); paintMessages(); render();
  }
  const painted = new Map();
  const paint = (id, html) => { if (painted.get(id) !== html) { el(id).innerHTML = html; painted.set(id, html); } };
  let decisionDetails = new Map();
  C.mount('advisor');
  const views = ['chat', 'holdings', 'discover'];
  function showView(view, focus = false) {
    if (!views.includes(view)) view = 'chat';
    document.body?.classList.toggle('chat-view', view === 'chat');
    el('view-chat').parentElement?.style.setProperty('--segment-index', views.indexOf(view));
    for (const name of views) {
      el('panel-' + name).hidden = name !== view;
      el('view-' + name).setAttribute('aria-selected', String(name === view));
      el('view-' + name).tabIndex = name === view ? 0 : -1;
    }
    el('panel-personal').hidden = view !== 'chat';
    el('panel-tools').hidden = view === 'chat';
    if (focus) el('view-' + view).focus();
  }
  for (const [index, name] of views.entries()) {
    el('view-' + name).addEventListener('click', () => showView(name));
    el('view-' + name).addEventListener('keydown', e => {
      const next = e.key === 'ArrowRight' ? (index + 1) % 3 : e.key === 'ArrowLeft' ? (index + 2) % 3
        : e.key === 'Home' ? 0 : e.key === 'End' ? 2 : null;
      if (next !== null) { e.preventDefault(); showView(views[next], true); }
    });
  }
  showView(new URLSearchParams(location.search).get('view'));
  if (window.visualViewport) {
    const fitKeyboard = () => document.body.style.setProperty('--app-height', window.visualViewport.height + 'px');
    window.visualViewport.addEventListener('resize', fitKeyboard);
    fitKeyboard();
  }
  const status = text => { el('advisor-status').textContent = text; };
  const money = n => n == null ? '—' : C.usd(n);
  const active = j => j.state === 'RUNNING' || j.state === 'QUEUED';
  function researchButton(symbol, account) {
    return '<button class="btn btn-secondary btn-sm" data-research="' + h(symbol) + '"' +
      (account ? ' data-account="' + h(account) + '"' : '') + '>' + t('ai.research') + '</button>';
  }
  function paintMessages() {
    const log = el('chat-messages');
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    el('chat-messages').innerHTML = shownMessages.length ? shownMessages.map(m =>
      '<div class="chat-message ' + (m.role === 'user' ? 'user' : 'assistant') + '">' +
      '<p class="eyebrow">' + t(m.role === 'user' ? 'ai.you' : 'ai.assistant') + '</p>' +
      '<div class="md">' + C.md(m.content) + '</div>' +
      (m.research_candidates || []).map(c => '<div class="advisor-row"><div><strong>' + h(c.symbol) +
        '</strong><p class="body-sm">' + h(c.reason) + '</p></div>' + researchButton(c.symbol) + '</div>').join('') +
      (m.role === 'user' && m.state !== 'COMPLETE' ? '<p class="body-sm">' + h(C.code(m.state)) + '</p>' : '') + '</div>'
    ).join('') : '<p class="body-muted">' + t('ai.noMessages') + '</p>';
    if (atBottom) log.scrollTop = log.scrollHeight;
  }
  async function loadConversation(signature) {
    if (conversationLoading === conversation || window.CT_DEMO) return;
    const selected = conversation;
    const epoch = ++conversationEpoch;
    conversationLoading = selected;
    try {
      const data = await A.fetchJSON('/advisor/conversations/' + encodeURIComponent(selected));
      if (selected !== conversation || epoch !== conversationEpoch) return;
      shownMessages = data.messages || [];
      const firstQuestion = shownMessages.find(m => m.role === 'user' && m.content?.trim());
      const entry = entryFor(selected);
      if (entry && !entry.title && firstQuestion) {
        entry.title = firstQuestion.content.trim().slice(0, 90); remember();
      }
      conversationSignature = signature;
      paintMessages();
    } catch (err) { if (epoch === conversationEpoch) status(t('ai.error', { error: err.message })); }
    finally { if (epoch === conversationEpoch) conversationLoading = false; }
  }
  function render() {
    restoreSession();
    const state = D.advisor || {}, snapshot = state.snapshot || {}, jobs = state.jobs || [];
    el('snapshot-status').textContent = snapshot.fetched_at
      ? t(state.stale ? 'ai.stale' : 'ai.fresh', { date: C.dateTime(snapshot.fetched_at) }) : t('ai.empty');
    const decisions = state.decisions || [];
    el('observed-accounts').innerHTML = (snapshot.accounts || []).map(account =>
      '<div style="margin-block:20px"><h3>' + h(account.label) + ' · ' + t('ai.readOnly') + '</h3>' +
      '<p class="body-sm">' + t('ai.value') + ' ' + money(account.equity_usd) + ' · ' + t('ai.cash') + ' ' + money(account.cash_usd) + '</p>' +
      (account.positions.length ? account.positions.map(p => {
        const decision = decisions.find(d => d.ticker === p.symbol && d.account_id === account.id);
        return '<div class="advisor-row"><div><strong>' + h(p.symbol) + '</strong> · ' + money(p.market_value_usd) +
          '<p class="body-sm body-muted">' + h(p.quantity) + ' · ' + t('ai.cost') + ' ' + money(p.average_cost) +
          (p.unrealized_pct == null ? '' : ' · ' + h(C.pct(p.unrealized_pct))) + '</p>' +
          '<p class="body-sm">' + h(decision ? C.code(decision.action) : t('ai.notReviewed')) + '</p></div>' +
          researchButton(p.symbol, account.id) + '</div>';
      }).join('') : '<p class="body-sm">' + t(account.complete ? 'ai.noPositions' : 'ai.empty') + '</p>') + '</div>'
    ).join('') + ((snapshot.errors || []).length ? '<p class="body-sm">' + h(t('ai.partial', { errors: snapshot.errors.join(' ') })) + '</p>' : '');
    el('review-holdings').disabled = !A.writesEnabled || state.stale || !(snapshot.accounts || []).some(a => a.complete && a.positions.length);
    paint('candidate-list', (snapshot.candidates || []).map(c =>
      '<div class="advisor-row"><div><strong>' + h(c.symbol) + '</strong><p class="body-sm body-muted">' +
      h(c.sources.join(' · ')) + '</p></div>' + researchButton(c.symbol) + '</div>').join(''));
    // Newest completed analysis per account/symbol. Historical conditions always carry a date.
    const seen = new Set();
    decisionDetails = new Map();
    paint('holding-decisions', decisions.filter(d => {
      const key = (d.account_id || d.bucket) + ':' + d.ticker;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 12).map(d => {
      const p = d.policy?.proposal || {};
      const detail = (key, value) => value ? '<dt>' + t(key) + '</dt><dd>' + h(value) + '</dd>' : '';
      const scope = d.account_id
        ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket);
      decisionDetails.set(d.run_id, '<div class="research-sheet-hero"><span class="ticker-orb" aria-hidden="true">' + h(d.ticker) +
        '</span><div><h3>' + h(d.ticker) + '</h3><p>' + h(C.code(d.action)) + ' · ' + h(d.trade_date) + '</p></div></div><p class="body-sm">' + h(d.account_id
          ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket)) +
        '</p><p>' + h(p.investment_thesis || '') + '</p><dl>' + detail('ai.entry', p.entry_logic) +
        detail('ai.exit', p.exit_logic) + detail('ai.risks', [...(p.primary_risks || []), ...(p.missing_information || [])].join('\n')) +
        '</dl><div class="advisor-actions"><a class="btn btn-secondary btn-sm" href="research.html?run=' + encodeURIComponent(d.run_id) +
        '">' + t('ai.fullReport') + '</a><a class="btn btn-secondary btn-sm" href="research.html?ticker=' + encodeURIComponent(d.ticker) +
        '">' + t('ai.formal') + '</a></div>');
      return '<button class="research-tile" data-decision="' + h(d.run_id) + '" aria-haspopup="dialog"><span class="ticker-orb" aria-hidden="true">' + h(d.ticker) +
        '</span><span class="utility-copy"><strong>' + h(d.ticker) + '</strong><span>' + h(scope) + ' · ' + h(d.trade_date) +
        '</span></span><span class="action-badge">' + h(C.code(d.action)) + '</span><span class="ux-chevron"></span></button>';
    }).join(''));
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
    if (state.notes && !notesDirty && (!notesLoaded || state.notes.revision !== notesRevision)) {
      el('personal-notes').value = state.notes.text;
      notesRevision = state.notes.revision; notesLoaded = true;
    }
    const choices = [...new Set([conversation, ...(state.conversations || []), ...Object.keys(session?.state.entries || {})])];
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
    el('notes-preview').textContent = state.notes?.text || t('ux.personalHint');
    const chatting = jobs.some(j => active(j) && j.task?.conversation_id === conversation);
    el('chat-send').disabled = busy || chatting || !A.writesEnabled;
    el('shortlist').disabled = busy || chatting || !A.writesEnabled;
    const signature = conversation + JSON.stringify(jobs.filter(j => j.task?.conversation_id === conversation).map(j => [j.run_key, j.state]));
    if (signature !== conversationSignature) loadConversation(signature);
    if (window.CT_DEMO) { status(t('ai.demo')); paintMessages(); }
  }
  async function send(kind, extra = {}) {
    if (busy) return;
    busy = true; el('chat-send').disabled = true;
    const payload = { kind, language: window.I18N.lang, conversation_id: conversation, ...extra };
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
      if (result.blocked) { status(t('ai.demo')); return; }
      if (pendingRequest === request) pendingRequest = null;
      const saved = entryFor(selected);
      if (kind === 'chat') {
        if (saved?.pending?.request_id === request.request_id) delete saved.pending;
        if (saved && !saved.title) saved.title = (extra.message || '').slice(0, 90);
        if (selected === conversation && el('chat-input').value === sentText) el('chat-input').value = '';
        else if (saved?.draft === sentText) saved.draft = '';
      }
      remember();
      status(t('ai.queued')); conversationSignature = '';
      await A.readOnce('advisor');
    } catch (err) { status(t('ai.error', { error: err.message })); }
    finally { busy = false; render(); }
  }
  el('chat-form').addEventListener('submit', e => {
    e.preventDefault(); const message = el('chat-input').value.trim();
    if (message) send('chat', { message });
  });
  el('chat-input').addEventListener('input', remember);
  el('ask-risk').addEventListener('click', () => { el('chat-input').value = t('ai.askRisk'); remember(); });
  el('ask-scenario').addEventListener('click', () => { el('chat-input').value = t('ai.askScenario'); remember(); });
  el('refresh-holdings').addEventListener('click', () => send('refresh'));
  el('shortlist').addEventListener('click', () => { showView('chat'); send('shortlist', { message: t('ai.shortlistPrompt') }); });
  el('symbol-form').addEventListener('submit', e => {
    e.preventDefault(); send('analysis', { ticker: el('symbol-input').value.trim().toUpperCase() });
  });
  el('new-conversation').addEventListener('click', () => {
    chooseConversation(crypto.randomUUID());
  });
  el('personal-notes').addEventListener('input', () => { notesDirty = true; remember(); });
  el('save-notes').addEventListener('click', async () => {
    if (el('save-notes').disabled) return;
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
      await A.readOnce('advisor');
    } catch (err) { el('notes-save-status').textContent = t('ai.error', { error: err.message }); }
    finally { el('save-notes').disabled = false; }
  });
  let reviewRequest = null;
  el('review-holdings').addEventListener('click', async () => {
    if (busy) return;
    busy = true; reviewRequest ||= crypto.randomUUID();
    try {
      const result = await A.post('/advisor/review', { request_id: reviewRequest, language: window.I18N.lang });
      if (result.blocked) { status(t('ai.demo')); return; }
      reviewRequest = null;
      status(t('ai.batch', { started: result.started.length, deferred: result.deferred.length }) + ' ' +
        result.deferred.map(d => d.symbol + ': ' + d.reason).join(' · '));
      await A.readOnce('advisor');
    } catch (err) { status(t('ai.error', { error: err.message })); }
    finally { busy = false; render(); }
  });
  document.addEventListener('click', async e => {
    const choice = e.target.closest?.('[data-conversation]');
    if (choice) {
      C.closeSheet('conversation-sheet'); chooseConversation(choice.dataset.conversation); return;
    }
    const decision = e.target.closest?.('[data-decision]');
    if (decision) {
      el('decision-detail').innerHTML = decisionDetails.get(decision.dataset.decision) || '';
      C.openSheet('decision-sheet'); return;
    }
    const button = e.target.closest?.('[data-research], [data-cancel]');
    if (!button || button.disabled) return;
    if (button.dataset.research) {
      await send('analysis', { ticker: button.dataset.research, account_id: button.dataset.account || null });
    } else {
      button.disabled = true;
      try {
        const result = await A.post('/research/' + encodeURIComponent(button.dataset.cancel) + '/cancel', {});
        if (result.blocked) { status(t('ai.demo')); return; }
        await A.readOnce('advisor');
      } catch (err) { status(t('ai.error', { error: err.message })); }
      finally { button.disabled = false; }
    }
  });
  document.addEventListener('ct:lang', paintMessages);
  document.addEventListener('ct:auth', e => {
    if (e.detail?.clearDrafts || (e.detail?.authenticated && session && session.state.owner !== e.detail.username)) {
      session?.clear(); session = null; notesDirty = false; notesLoaded = false;
      conversation = 'default'; pendingRequest = null; shownMessages = []; conversationSignature = '';
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
  const revealPersonalContext = () => {
    if (window.location.hash === '#personal-sheet') C.openSheet('personal-sheet');
  };
  revealPersonalContext();
  window.addEventListener?.('hashchange', revealPersonalContext);
  C.live(['advisor'], render);
})();
