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
    conversation = id; shownMessages = []; conversationSignature = '';
    conversationEpoch++; conversationLoading = false; conversationBlocked = false;
    el('chat-input').value = entryFor(id)?.draft || '';
    pendingRequest = entryFor(id)?.pending || null;
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
  const views = ['chat', 'discover'];
  function showView(view, focus = false) {
    if (binding.scope === 'agentic') view = 'chat';
    if (!views.includes(view)) view = 'chat';
    document.body?.classList.toggle('chat-view', view === 'chat');
    for (const name of views) {
      el('panel-' + name).hidden = name !== view;
      el('view-' + name).setAttribute('aria-selected', String(name === view));
      el('view-' + name).tabIndex = name === view ? 0 : -1;
    }
    el('panel-personal').hidden = view !== 'chat' || binding.scope !== 'personal';
    el('panel-tools').hidden = view === 'chat' || binding.scope !== 'personal';
    if (focus) el('view-' + view).focus();
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
  let entryPromptPending = params.get('prompt') === 'holdings-risk';
  if (params.get('view') === 'holdings') location.replace('portfolio.html' + (window.CT_DEMO ? '?demo=1' : ''));
  showView(params.get('view'));
  if (window.visualViewport) {
    const fitKeyboard = () => document.body.style.setProperty('--app-height', window.visualViewport.height + 'px');
    window.visualViewport.addEventListener('resize', fitKeyboard);
    fitKeyboard();
  }
  const status = text => { el('advisor-status').textContent = text; };
  const active = j => j.state === 'RUNNING' || j.state === 'QUEUED';
  function updateAccountLink() {
    const value = el('assistant-account').value;
    accountChoice = value;
    const q = new URLSearchParams({ scope: value === 'agentic' ? 'agentic' : 'personal' });
    if (value.startsWith('personal:') && value.slice(9)) q.set('account', value.slice(9));
    if (window.CT_DEMO) q.set('demo', '1');
    el('assistant-switch').href = 'advisor.html?' + q;
  }
  el('assistant-account').addEventListener('change', updateAccountLink);
  function renderScope(state) {
    const options = [['personal:', t('ai.allPersonal')], ['agentic', 'Agentic']];
    (D.accounts?.accounts || []).filter(row => row.scope === 'personal' && row.account_scope_kind === 'single')
      .forEach(row => options.push(['personal:' + row.account_id, row.account_label || row.account_id]));
    if (!options.some(([key]) => key === accountChoice)) options.push([accountChoice, binding.account || t('ai.scopeMismatch')]);
    paint('assistant-account', options.map(([key, label]) => '<option value="' + h(key) + '">' + h(label) + '</option>').join(''));
    el('assistant-account').value = accountChoice; updateAccountLink();
    el('assistant-scope-note').textContent = (state.account_label || binding.account || (binding.scope === 'agentic' ? 'Agentic' : t('ai.allPersonal'))) + ' · ' +
      t(binding.scope === 'agentic' ? 'ai.agenticOnly' : 'ai.personalOnly');
    const source = A.state?.resources?.assistant;
    el('assistant-account-status').textContent = !['personal', 'agentic'].includes(binding.scope) ? t('ai.scopeMismatch') : !knownAccount() && D.accounts ? t('ai.unknownAccount') : !currentState() ? source?.error || t('common.loading')
      : !A.isLive('assistant') ? t('ai.scopeStale') : !A.isLive('accounts') || D.accounts?.personal_snapshot?.stale && binding.scope === 'personal'
        ? t('rs.accountsPartial') : '';
    el('assistant-coverage').textContent = state.coverage_info?.note || state.coverage || '';
    el('assistant-research-link').href = scopeLink('research');
    el('assistant-intro').textContent = t(binding.scope === 'agentic' ? 'ai.agenticOnly' : 'ai.intro');
    el('assistant-chat-note').textContent = t(binding.scope === 'agentic' ? 'ai.agenticOnly' : 'ai.scopedChatNote');
    el('view-discover').hidden = binding.scope !== 'personal';
    el('agentic-records-panel').hidden = binding.scope !== 'agentic';
    document.querySelectorAll('[data-open-sheet="personal-sheet"]').forEach(button => { button.hidden = binding.scope !== 'personal'; });
    el('panel-personal').hidden = binding.scope !== 'personal' || el('panel-chat').hidden;
    el('review-panel').hidden = binding.scope !== 'personal';
    const review = state.latest_review;
    el('review-summary').textContent = review ? [review.account_label,
      t('ai.reviewTimes', { snapshot: C.dateTime(review.snapshot_at), analysed: C.dateTime(review.analysed_at) })].filter(Boolean).join(' · ') : t('ai.noReview');
    el('review-stale').textContent = review?.stale ? t('ai.reviewStale', { current: C.dateTime(state.snapshot?.fetched_at) }) : '';
    el('review-open').hidden = !review?.run_key;
  }
  function reviewMarkup(review) {
    const citation = item => item.kind === 'run' && item.run_id ? '<a href="' + h(scopeLink('research', { run: item.run_id, include_all_personal: 'true' })) + '">' + h(item.run_id) + '</a>' : h([item.kind, item.as_of].filter(Boolean).join(' · '));
    return '<h3>' + h(review.account_label || t('ai.portfolioReview')) + '</h3><p class="body-sm">' +
      h(t('ai.reviewTimes', { snapshot: C.dateTime(review.snapshot_at), analysed: C.dateTime(review.analysed_at) })) + '</p>' +
      (review.stale ? '<p class="banner">' + t('ai.reviewOld') + '</p>' : '') +
      (review.priorities || []).map(item => '<article class="card" style="margin-block:16px;"><h3>' + h(item.symbol) + '</h3><p>' + h(item.recommendation) + '</p><p>' + h(item.rationale) +
        '</p><p class="body-sm">' + h((item.missing || []).join('; ')) + '</p><p class="body-sm">' + (item.citations || []).map(citation).join(' · ') + '</p></article>').join('') +
      '<p class="body-sm">' + h((review.limitations || []).join('; ')) + '</p>';
  }
  el('review-open').addEventListener('click', async () => {
    const review = currentState()?.latest_review;
    if (!review?.run_key || binding.scope !== 'personal') return;
    const epoch = scopeEpoch, run = review.run_key;
    shownDecision = run; shownDecisionJob = null; decisionReadAttempted = false;
    el('decision-detail').innerHTML = reviewMarkup(review); C.openSheet('decision-sheet');
    if (!window.CT_DEMO) {
      try { const job = await A.fetchJSON('/research/' + encodeURIComponent(run));
        if (epoch === scopeEpoch && shownDecision === run) { shownDecisionJob = job; markDecisionRead(); }
      } catch (error) { if (epoch === scopeEpoch) status(t('rs.openedFailed', { err: error.message })); }
    }
  });
  function researchButton(symbol, account) {
    if (binding.scope === 'agentic') return '<a class="btn btn-secondary btn-sm" href="' + h(scopeLink('research', { ticker: symbol })) + '">' + t('ai.research') + '</a>';
    return '<button type="button" class="btn btn-secondary btn-sm" data-research="' + h(symbol) + '"' +
      (account ? ' data-account="' + h(account) + '"' : '') + '>' + t('ai.research') + '</button>';
  }
  function paintMessages() {
    const log = el('chat-messages');
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 60;
    paint('chat-messages', shownMessages.length ? shownMessages.map(m =>
      '<div class="chat-message ' + (m.role === 'user' ? 'user' : 'assistant') + '">' +
      '<p class="eyebrow">' + t(m.role === 'user' ? 'ai.you' : 'ai.assistant') + '</p>' +
      '<div class="md">' + C.md(m.content) + '</div>' +
      (m.research_candidates || []).map(c => '<div class="advisor-row"><div><strong>' + h(c.symbol) +
        '</strong><p class="body-sm">' + h(c.reason) + '</p></div>' + researchButton(c.symbol) + '</div>').join('') +
      (m.role === 'user' && m.state !== 'COMPLETE' ? '<p class="body-sm">' + h(C.code(m.state)) + '</p>' : '') + '</div>'
    ).join('') : '<p class="body-muted">' + t('ai.noMessages') + '</p>');
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
    finally { if (epoch === conversationEpoch) { conversationLoading = false; el('chat-send').disabled = conversationBlocked || busy || !available(); el('shortlist').disabled = conversationBlocked || busy || binding.scope !== 'personal' || !available(); } }
  }
  function render() {
    restoreSession();
    if (entryPromptPending) {
      if (!el('chat-input').value) el('chat-input').value = t('ai.askRisk');
      remember(); entryPromptPending = false;
    }
    const state = currentState() || {}, snapshot = state.snapshot || {}, jobs = state.jobs || [];
    renderScope(state);
    const decisions = state.decisions || [];
    paint('candidate-list', (snapshot.candidates || []).map(c =>
      '<div class="advisor-row"><div><strong>' + h(c.symbol) + '</strong><p class="body-sm body-muted">' +
      h(c.sources.join(' · ')) + '</p></div>' + researchButton(c.symbol) + '</div>').join(''));
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
      decisionDetails.set(d.run_id, '<div class="research-sheet-hero"><span class="ticker-orb" aria-hidden="true">' + h(d.ticker) +
        '</span><div><h3>' + h(d.ticker) + '</h3><p>' + h(C.code(d.action)) + ' · ' + h(d.trade_date) + '</p></div></div><p class="body-sm">' + h(d.account_id
          ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket)) +
        '</p><p>' + h(p.investment_thesis || '') + '</p><dl>' + detail('ai.entry', p.entry_logic) +
        detail('ai.exit', p.exit_logic) + detail('ai.risks', [...(p.primary_risks || []), ...(p.missing_information || [])].join('\n')) +
        '</dl><div class="advisor-actions"><a class="btn btn-secondary btn-sm" href="' + h(scopeLink('research', { run: d.run_id, account: binding.scope === 'personal' && d.account_scope_kind !== 'all_personal' ? d.account_id : '', include_all_personal: d.account_scope_kind === 'all_personal' ? 'true' : '' })) +
        '">' + t('ai.fullReport') + '</a>' + researchButton(d.ticker, d.account_id) + '</div>');
      return '<button class="research-tile" data-decision="' + h(d.run_id) + '" aria-haspopup="dialog"><span class="ticker-orb" aria-hidden="true">' + h(d.ticker) +
        '</span><span class="utility-copy"><strong>' + h(d.ticker) + '</strong><span>' + h(scope) + ' · ' + h(d.trade_date) +
        '</span></span><span class="action-badge">' + h(C.code(d.action)) + '</span><span class="ux-chevron"></span></button>';
    }).join('');
    paint('holding-decisions', decisionCards); paint('agentic-records', decisionCards);
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
    el('chat-send').disabled = busy || chatting || conversationBlocked || !available();
    el('shortlist').disabled = busy || chatting || conversationBlocked || binding.scope !== 'personal' || !available();
    el('review-start').disabled = busy || binding.scope !== 'personal' || !available() || jobs.some(j => active(j) && j.task?.kind === 'portfolio_review');
    document.querySelectorAll('[data-research], #symbol-form button').forEach(button => { button.disabled = busy || binding.scope !== 'personal' || !available(); });
    const signature = conversation + JSON.stringify(jobs.filter(j => j.task?.conversation_id === conversation).map(j => [j.run_key, j.state]));
    if (currentState() && signature !== conversationSignature) loadConversation(signature);
    if (window.CT_DEMO) { status(t('ai.demo')); paintMessages(); }
  }
  async function send(kind, extra = {}) {
    if (busy || !available() || binding.scope === 'agentic' && kind !== 'chat' ||
        ['chat', 'shortlist'].includes(kind) && conversationBlocked || binding.account && extra.account_id && extra.account_id !== binding.account) return;
    busy = true; status(t('ai.running')); render();
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
      await A.readOnce('assistant');
    } catch (err) { if (session === sendSession) { if (err.status === 409) status(t('ai.conflict') + ' ' + err.message); else status(t('ai.error', { error: err.message })); } }
    finally { busy = false; render(); }
  }
  el('chat-form').addEventListener('submit', e => {
    e.preventDefault(); const message = el('chat-input').value.trim();
    if (message) send('chat', { message });
  });
  el('chat-input').addEventListener('input', remember);
  el('ask-risk').addEventListener('click', () => { el('chat-input').value = t('ai.askRisk'); remember(); });
  el('ask-scenario').addEventListener('click', () => { el('chat-input').value = t('ai.askScenario'); remember(); });
  el('shortlist').addEventListener('click', () => { showView('chat'); send('shortlist', { message: t('ai.shortlistPrompt') }); });
  el('review-start').addEventListener('click', () => send('portfolio_review'));
  el('symbol-form').addEventListener('submit', e => {
    e.preventDefault(); send('analysis', { ticker: el('symbol-input').value.trim().toUpperCase() });
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
    const next = new URLSearchParams(location.search);
    const selected = { scope: next.get('scope') || 'personal', account: next.get('account') || '' };
    if (selected.scope === 'agentic') selected.account = '';
    const changed = JSON.stringify(selected) !== JSON.stringify(binding);
    if (changed) {
      remember(); binding = selected; scopeEpoch++; conversationEpoch++; session = null;
      conversation = 'default'; conversationLoading = false; conversationSignature = ''; conversationBlocked = false;
      shownMessages = []; shownDecision = null; shownDecisionJob = null; pendingRequest = null;
      notesLoaded = false; notesDirty = false; notesRevision = 0;
      el('chat-input').value = ''; el('personal-notes').value = ''; status('');
      ['personal-sheet', 'conversation-sheet', 'decision-sheet', 'jobs-sheet'].forEach(id => el(id).close());
      document.querySelectorAll('#review-panel details, #agentic-records-panel details').forEach(node => { node.open = false; });
      paintMessages();
    }
    accountChoice = binding.scope === 'agentic' ? 'agentic' : 'personal:' + binding.account;
    requestedConversation = next.get('conversation');
    A.selectAssistant(binding);
    restoreSession();
    if (requestedConversation && session?.validId(requestedConversation)) { chooseConversation(requestedConversation); requestedConversation = null; }
    if (changed || next.has('view')) showView(next.get('view'));
    if (next.get('sheet') === 'jobs') C.openSheet('jobs-sheet');
    if (next.get('prompt') === 'holdings-risk') {
      showView('chat');
      entryPromptPending = true;
    }
    render();
  }
  document.addEventListener('ct:route', routeEntry);
  routeEntry();
  C.live([], render, ['assistant', 'accounts']);
});
