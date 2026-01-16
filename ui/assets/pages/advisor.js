(function () {
  'use strict';
  const C = window.Console, A = window.CT_API, D = C.D;
  const el = id => document.getElementById(id), h = C.escapeHtml, t = C.t;
  let conversation = 'default', conversationSignature = '', conversationLoading = false;
  let notesRevision = 0, notesLoaded = false, notesDirty = false;
  let busy = false, pendingRequest = null, shownMessages = [];
  C.mount('advisor');
  const status = text => { el('advisor-status').textContent = text; };
  const money = n => n == null ? '—' : C.usd(n);
  const active = j => j.state === 'RUNNING' || j.state === 'QUEUED';
  function researchButton(symbol, account) {
    return '<button class="btn btn-secondary btn-sm" data-research="' + h(symbol) + '"' +
      (account ? ' data-account="' + h(account) + '"' : '') + '>' + t('ai.research') + '</button>';
  }
  function paintMessages() {
    el('chat-messages').innerHTML = shownMessages.length ? shownMessages.map(m =>
      '<div class="chat-message ' + (m.role === 'user' ? 'user' : 'assistant') + '">' +
      '<p class="eyebrow">' + t(m.role === 'user' ? 'ai.you' : 'ai.assistant') + '</p>' +
      '<div class="md">' + C.md(m.content) + '</div>' +
      (m.research_candidates || []).map(c => '<div class="advisor-row"><div><strong>' + h(c.symbol) +
        '</strong><p class="body-sm">' + h(c.reason) + '</p></div>' + researchButton(c.symbol) + '</div>').join('') +
      (m.role === 'user' && m.state !== 'COMPLETE' ? '<p class="body-sm">' + h(C.code(m.state)) + '</p>' : '') + '</div>'
    ).join('') : '<p class="body-muted">' + t('ai.noMessages') + '</p>';
  }
  async function loadConversation(signature) {
    if (conversationLoading || window.CT_DEMO) return;
    const selected = conversation;
    conversationLoading = true;
    try {
      const data = await A.fetchJSON('/advisor/conversations/' + encodeURIComponent(selected));
      if (selected !== conversation) return;
      shownMessages = data.messages || [];
      conversationSignature = signature;
      paintMessages();
    } catch (err) { status(t('ai.error', { error: err.message })); }
    finally { conversationLoading = false; }
  }
  function render() {
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
    el('candidate-list').innerHTML = (snapshot.candidates || []).map(c =>
      '<div class="advisor-row"><div><strong>' + h(c.symbol) + '</strong><p class="body-sm body-muted">' +
      h(c.sources.join(' · ')) + '</p></div>' + researchButton(c.symbol) + '</div>').join('');
    // Newest completed analysis per account/symbol. Historical conditions always carry a date.
    const seen = new Set();
    el('holding-decisions').innerHTML = decisions.filter(d => {
      const key = (d.account_id || d.bucket) + ':' + d.ticker;
      if (seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 12).map(d => {
      const p = d.policy?.proposal || {};
      const detail = (key, value) => value ? '<dt>' + t(key) + '</dt><dd>' + h(value) + '</dd>' : '';
      return '<details class="advisor-decision"><summary>' + h(d.ticker) + ' · ' + h(C.code(d.action)) +
        ' · ' + h(d.trade_date) + '</summary><p class="body-sm">' + h(d.account_id
          ? (snapshot.accounts || []).find(a => a.id === d.account_id)?.label || t('ai.readOnly') : C.bucket(d.bucket)) +
        '</p><p>' + h(p.investment_thesis || '') + '</p><dl>' + detail('ai.entry', p.entry_logic) +
        detail('ai.exit', p.exit_logic) + detail('ai.risks', [...(p.primary_risks || []), ...(p.missing_information || [])].join('\n')) +
        '</dl><div class="advisor-actions"><a class="btn btn-secondary btn-sm" href="research.html?run=' + encodeURIComponent(d.run_id) +
        '">' + t('ai.fullReport') + '</a><a class="btn btn-secondary btn-sm" href="research.html?ticker=' + encodeURIComponent(d.ticker) +
        '">' + t('ai.formal') + '</a></div></details>';
    }).join('');
    el('advisor-jobs').innerHTML = jobs.slice(0, 8).map(j => '<div class="advisor-row"><div>' + h(j.ticker) +
      ' · ' + h(C.code(j.state)) + '<p class="body-sm body-muted">' + h(j.error || '') + '</p></div>' +
      (active(j) ? '<button class="btn btn-secondary btn-sm" data-cancel="' + h(j.run_key) + '">' + t('ai.cancel') + '</button>' : '') + '</div>').join('');
    if (state.notes && !notesDirty && (!notesLoaded || state.notes.revision !== notesRevision)) {
      el('personal-notes').value = state.notes.text;
      notesRevision = state.notes.revision; notesLoaded = true;
    }
    const choices = [...new Set([conversation, ...(state.conversations || [])])];
    el('conversations').innerHTML = choices.map(id => '<option value="' + h(id) + '"' +
      (id === conversation ? ' selected' : '') + '>' + h(id === 'default' ? t('ai.chat') : id.slice(0, 8)) + '</option>').join('');
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
    try {
      const result = await A.post('/advisor/jobs', { ...payload, request_id: pendingRequest.request_id });
      if (result.blocked) { status(t('ai.demo')); return; }
      pendingRequest = null;
      if (kind === 'chat' || kind === 'shortlist') el('chat-input').value = '';
      status(t('ai.queued')); conversationSignature = '';
      await A.readOnce('advisor');
    } catch (err) { status(t('ai.error', { error: err.message })); }
    finally { busy = false; render(); }
  }
  el('chat-form').addEventListener('submit', e => {
    e.preventDefault(); const message = el('chat-input').value.trim();
    if (message) send('chat', { message });
  });
  el('ask-risk').addEventListener('click', () => { el('chat-input').value = t('ai.askRisk'); });
  el('ask-scenario').addEventListener('click', () => { el('chat-input').value = t('ai.askScenario'); });
  el('refresh-holdings').addEventListener('click', () => send('refresh'));
  el('shortlist').addEventListener('click', () => send('shortlist', { message: t('ai.shortlistPrompt') }));
  el('symbol-form').addEventListener('submit', e => {
    e.preventDefault(); send('analysis', { ticker: el('symbol-input').value.trim().toUpperCase() });
  });
  el('new-conversation').addEventListener('click', () => {
    conversation = crypto.randomUUID(); shownMessages = []; conversationSignature = ''; paintMessages(); render();
  });
  el('conversations').addEventListener('change', () => {
    conversation = el('conversations').value; shownMessages = []; conversationSignature = ''; paintMessages(); render();
  });
  el('personal-notes').addEventListener('input', () => { notesDirty = true; });
  el('save-notes').addEventListener('click', async () => {
    if (el('save-notes').disabled) return;
    el('save-notes').disabled = true;
    const text = el('personal-notes').value;
    try {
      const saved = await A.post('/advisor/notes', { text, revision: notesRevision });
      if (saved.blocked) { status(t('ai.demo')); return; }
      notesRevision = saved.revision;
      notesDirty = el('personal-notes').value !== text;
      status(t('ai.saved'));
      await A.readOnce('advisor');
    } catch (err) { status(t('ai.error', { error: err.message })); }
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
  C.live(['advisor'], render);
})();
