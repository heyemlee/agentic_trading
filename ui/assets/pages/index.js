window.Console.page('index', function (document) {
  const C = window.Console, D = C.D, API = window.CT_API;
  C.mount('dashboard');
  const $ = id => document.getElementById(id);
  const h = C.escapeHtml;

  /* Home is the accounts. Above them, and only while there is something, come what needs you
     and what is not normal; a quiet day shows the accounts alone. It was the Assets tab until
     2026-09-23, when Today's to-dos moved onto it and the tab bar became Home, Lab and Me:
     research is the Lab's, and the notices System holds are counted on Me.

     The URL selects one account view and keeps direct links stable. A Robinhood holding is
     one more level of the same address — index.html?scope=robinhood&account=<id>&ticker=<symbol>
     — so it has a link of its own, Back returns to the list, and a reload lands on it again.
     portfolio.html, the address these had before, still leads here (navigation.js, api/app.py). */
  const routeScope = () => {
    const requested = new URLSearchParams(location.search).get('scope');
    return requested === 'agentic' || (requested === 'demo' && C.SHOW_CONCEPTUAL)
      ? requested : 'robinhood';
  };
  const routeHolding = () => {
    const query = new URLSearchParams(location.search);
    const account = query.get('account') || '', ticker = (query.get('ticker') || '').toUpperCase();
    return routeScope() === 'robinhood' && /^[0-9a-f]{20}$/.test(account) && /^[A-Z][A-Z0-9.-]{0,19}$/.test(ticker)
      ? { account, ticker } : null;
  };
  const sameHolding = (a, b) => !!a && !!b && a.account === b.account && a.ticker === b.ticker;
  let scope = routeScope(), holding = routeHolding();

  let refreshing = false, refreshRequest = null;

  /* The Robinhood block is a saved snapshot, written by a refresh job, never
     by this page's polling reads. Offer an explicit refresh beside its time,
     and ask once per visit automatically when the saved snapshot is stale.

     A refresh is not free: it is a worker process out of the same concurrency
     pool as research runs, and it leaves a job and a log behind. So it is
     asked for only with the tab in front, never while one is already queued or
     running, and never twice inside a minute of tab-flipping. */
  const ACTIVE = (job) => job.state === 'QUEUED' || job.state === 'RUNNING';
  const refreshJobs = (saved) => (saved.jobs || []).filter((job) => (job.task || {}).kind === 'refresh');
  let armed = true, askedAt = 0, askedFor = null, askError = '';
  document.addEventListener('visibilitychange', () => { if (document.hidden) armed = true; });

  async function askRefresh(saved, manual = false) {
    if (scope !== 'robinhood') return;
    if (refreshing || (!armed && !manual) || document.hidden || !API.writesEnabled || !API.isLive('advisor')) return;
    if (refreshJobs(saved).some(ACTIVE) || (!manual && (!saved.stale || Date.now() - askedAt < 60000))) return;
    armed = false; askedAt = Date.now(); askError = ''; refreshing = true;
    refreshRequest ||= crypto.randomUUID();
    render();
    try {
      const job = await API.post('/advisor/jobs', {
        kind: 'refresh', language: window.I18N.lang, request_id: refreshRequest });
      if (job.blocked) return;
      askedFor = job.run_key; refreshRequest = null;
      await API.readOnce('advisor');
    } catch (error) { askError = error.message; }
    finally { refreshing = false; render(); }
  }

  $('refresh-holdings').addEventListener('click', () => askRefresh(D.advisor || {}, true));

  /* The strategy figures come from the ledgers, and a ledger fails closed once nothing
     has confirmed it for max_age_days. The API then answers with its buckets emptied and
     the reason in errors, which is why this page shows a gap rather than a zero — a zero
     would be a claim about money that is still there. Confirming is a check that can
     refuse, the same one ct-state confirm makes, so this button is a request to run it,
     not an override of it. */
  let confirming = false, confirmStatus = '';
  $('confirm-ledger').addEventListener('click', async () => {
    if (confirming || !API.writesEnabled) return;
    confirming = true; confirmStatus = C.t('pf.confirming'); render();
    try {
      const ledger = scope === 'demo' ? 'conceptual' : 'micro_live';
      const report = await API.post('/portfolio/confirm', { ledger });
      const refusals = [...(report.ledgers || []).filter(l => l.ledger === ledger && l.error).map(l => l.error),
                        ...(report.unsettled_orders || []).map(o => o.client_order_id)];
      confirmStatus = report.confirmed ? C.t('pf.confirmed')
        : C.t('pf.confirmRefused', { error: refusals.join(' · ') || report.detail || '' });
      await API.readOnce('portfolio');
    } catch (error) {
      confirmStatus = C.t('pf.confirmRefused', { error: error.message });
    } finally { confirming = false; loadCheck(); render(); }
  });

  /* The last ledger check, whoever ran it — the 03:45 job, ct-state confirm, the button
     above — read from its report rather than by opening the venue again. A refusal over
     cash carries the figure, and the two buttons record that figure as what the reader says
     it was: money they moved, which is never return, or income and fees, which are. The
     amount is never typed and never guessed, and a second tap confirms before anything is
     sent. Then the same check runs again, so agreement is proven rather than assumed. */
  let check = {}, armedKind = null, booking = false, bookStatus = '';
  async function loadCheck() {
    if (window.CT_DEMO || !API.fetchJSON) return;
    try { check = (await API.fetchJSON('/portfolio/ledgers')) || {}; } catch (_) { check = {}; }
    render();
  }
  const selectedLedger = () => scope === 'demo' ? 'conceptual' : 'micro_live';
  const refusedRow = () => (check.ledgers || []).find((l) => l.ledger === selectedLedger() && l.error && l.cash_difference_usd != null);
  async function book(kind) {
    const row = refusedRow();
    if (!row || booking || !API.writesEnabled) return;
    if (armedKind !== kind) { armedKind = kind; render(); return; }
    armedKind = null; booking = true; bookStatus = C.t('pf.booking'); render();
    const diff = row.cash_difference_usd;
    const what = kind === 'transfer' ? (diff > 0 ? 'deposit' : 'withdrawal') : (diff > 0 ? 'income' : 'fee');
    try {
      await API.post('/portfolio/cashflow', { bucket: row.bucket, amount_usd: Math.abs(diff), kind: what,
        reason: 'recorded from the ledger check of ' + (check.date || '') });
      const report = await API.post('/portfolio/confirm', { ledger: row.ledger });
      bookStatus = report.confirmed ? C.t('pf.booked')
        : C.t('pf.confirmRefused', { error: (report.ledgers || []).filter((l) => l.error).map((l) => l.error).join(' · ') || report.detail || '' });
      await API.readOnce('portfolio');
    } catch (error) { bookStatus = C.t('pf.bookFailed', { error: error.message }); }
    finally { booking = false; await loadCheck(); }
  }
  $('book-transfer').addEventListener('click', () => book('transfer'));
  $('book-income').addEventListener('click', () => book('income'));

  /* Analysing a holding is the Assistant's own request: the whole research team on one
     position, read-only, bound by the server to the account it is about. It spends one of
     the day's full analyses, so a second tap while the first is in flight sends nothing, and
     the request id is kept for an explicit retry of the same request after a lost response. */
  let analyzing = false, analyzeRequest = null, analyzeStatus = '';
  async function analyze() {
    const target = holding;
    if (!target || analyzing || !holdingState(target).canAnalyze) return;
    const key = target.account + ':' + target.ticker;
    if (!analyzeRequest || analyzeRequest.key !== key) analyzeRequest = { key, id: crypto.randomUUID() };
    analyzing = true; analyzeStatus = ''; render();
    try {
      const job = await API.post('/advisor/jobs', { kind: 'analysis', scope: 'personal', ticker: target.ticker,
        account_id: target.account, request_id: analyzeRequest.id, language: window.I18N.lang });
      if (job.blocked) { analyzeStatus = C.t('hd.demo'); return; }
      analyzeRequest = null; analyzeStatus = C.t('hd.requested');
      await API.readOnce('advisor');
    } catch (error) { analyzeStatus = C.t('hd.requestFailed', { error: error.message }); }
    finally { analyzing = false; render(); }
  }
  $('holding-analyze').addEventListener('click', analyze);
  /* Stopping is the server's decision to make: a run that finished in the meantime answers
     with a conflict, which is reported rather than swallowed. */
  let stopping = null;
  $('holding-job').addEventListener('click', async (event) => {
    const button = event.target.closest && event.target.closest('[data-stop]');
    if (!button || stopping) return;
    stopping = button.getAttribute('data-stop'); render();
    try {
      const result = await API.post('/research/' + encodeURIComponent(stopping) + '/cancel', {});
      if (result.blocked) analyzeStatus = C.t('hd.demo');
      else await API.readOnce('advisor');
    } catch (error) { analyzeStatus = C.t('hd.stopFailed', { error: error.message }); }
    finally { stopping = null; render(); }
  });

  /* ── shared formatting ──────────────────────────────────────────── */
  const money = value => value == null ? '—' : C.usd(value);
  const tone = n => n > 0 ? ' is-up' : n < 0 ? ' is-down' : '';
  const signedUsd = n => (n > 0 ? '+' : '') + C.usd(n);
  const signedPct = (n, digits) => (n > 0 ? '+' : '') + C.pct(n, digits);
  const shares = q => Number(q).toLocaleString('en-US', { maximumFractionDigits: 6 });
  const link = params => 'index.html?' + new URLSearchParams(params) + (window.CT_DEMO ? '&demo=1' : '');
  const reportLink = (runId, account) => 'lab.html?' + new URLSearchParams({ scope: 'personal', account,
    include_all_personal: 'true', run: runId }) + (window.CT_DEMO ? '&demo=1' : '');
  const stat = (label, value) => '<div class="pf-stat"><dt>' + h(label) + '</dt><dd>' + h(value) + '</dd></div>';
  const delta = (n, text, label) => n == null ? '' : '<span class="pf-delta' + tone(n) + '">' + h(text) + '</span>' +
    (label ? ' <span class="pf-delta-label">' + h(label) + '</span>' : '');

  /* ── one Robinhood account: its figure, then its holdings by value ───
     Each row also says where research stands on the holding -- being studied, what the last
     analysis concluded, or never studied -- so the list shows which one wants a look without
     opening each. It offers no button to start one: an analysis spends one of the day's full
     analyses, and a row is easy to tap in passing while scrolling. Starting it is one tap
     further, on the holding's own view, beside the result it would replace. */
  const rowDate = (iso) => {
    const day = new Date(iso);
    return day.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC',
      ...(day.getUTCFullYear() === new Date().getUTCFullYear() ? {} : { year: 'numeric' }) });
  };
  function researchState(account, p) {
    const x = holdingState({ account: account.id, ticker: p.symbol });
    if (x.running) return '<span class="pf-research is-running"><span class="pf-dot" aria-hidden="true"></span>' +
      h(C.t(x.running.state === 'QUEUED' ? 'hd.rowQueued' : 'hd.rowRunning')) + '</span>';
    if (x.failed) return '<span class="pf-research is-failed">' + h(C.t('hd.rowFailed')) + '</span>';
    if (!x.latest) return '<span class="pf-research">' + h(C.t('hd.rowNone')) + '</span>';
    return '<span class="pf-research"><strong>' + h(x.latest.action ? C.code(x.latest.action) : C.t('ac.incomplete')) +
      '</strong>' + (x.latest.trade_date ? ' · ' + h(rowDate(x.latest.trade_date)) : '') + '</span>';
  }
  function holdingRow(account, p) {
    return '<a class="pf-row" href="' + h(link({ scope: 'robinhood', account: account.id, ticker: p.symbol })) +
      '" data-od-id="holding-' + h(p.symbol) + '">' +
      '<span class="pf-row-main"><span class="pf-row-title">' + h(p.symbol) + '</span>' +
        '<span class="pf-row-sub">' + h(C.t('hd.sharesN', { n: shares(p.quantity) })) + ' · ' + researchState(account, p) + '</span></span>' +
      '<span class="pf-row-side"><span class="pf-row-value">' + money(p.market_value_usd) + '</span>' +
        (p.unrealized_pct == null ? '<span class="pf-delta">—</span>' : delta(p.unrealized_pct, signedPct(p.unrealized_pct))) +
      '</span><span class="pf-chevron" aria-hidden="true"></span></a>';
  }
  function accountCard(a) {
    const positions = [...(a.positions || [])].sort((x, y) => (y.market_value_usd ?? -1) - (x.market_value_usd ?? -1));
    return '<article class="pf-card pf-account" data-od-id="account-' + h(a.id || a.label) + '">' +
      '<div class="pf-card-head"><p class="pf-eyebrow">' + h(C.t('pf.accountLabel', { label: a.label })) + '</p>' +
        '<span class="pf-badge">' + h(C.t('ai.readOnly')) + '</span></div>' +
      '<p class="pf-figure">' + money(a.equity_usd) + '</p>' +
      '<p class="pf-fine">' + h(C.t('ai.cash') + ' ' + money(a.cash_usd) + ' · ' + C.t('pf.positions', { n: positions.length })) + '</p>' +
      (!a.complete ? '<p class="pf-warn">' + h(C.t('ux.incompleteAccount')) + '</p>' : '') +
      (positions.length ? '<div class="pf-rows pf-holdings">' + positions.map(p => holdingRow(a, p)).join('') + '</div>'
        : a.complete ? '<p class="pf-fine pf-empty-line">' + h(C.t('ai.noPositions')) + '</p>' : '') +
    '</article>';
  }

  /* ── one holding: the position, and what the research concluded about it ─
     Everything here is read from the /advisor state this page already polls: the snapshot
     for the position, the recorded decisions for the analyses, and the queue for a run in
     flight. Records that never said which account they were about are left out, as the
     research page leaves them out of a single-account view; an all-accounts analysis of the
     same symbol is kept and says so. */
  function holdingState(target) {
    const saved = D.advisor || {}, snapshot = saved.snapshot || {};
    const account = (snapshot.accounts || []).find(a => a.id === target.account) || null;
    const position = account && (account.positions || []).find(p => p.symbol === target.ticker) || null;
    const analyses = (saved.decisions || []).filter(d => d.ticker === target.ticker &&
      (d.account_scope_kind === 'single' ? d.account_id === target.account : d.account_scope_kind === 'all_personal'));
    const jobs = (saved.jobs || []).filter(j => j.ticker === target.ticker && (j.task || {}).kind === 'analysis' &&
      ((j.task.account_scope_kind || 'single') === 'single' ? j.task.account_id === target.account
        : (j.task.account_ids || []).includes(target.account)));
    const running = jobs.find(ACTIVE) || null;
    const latest = analyses[0] || null;
    const failed = !running && jobs[0] && jobs[0].state !== 'COMPLETE' &&
      (!latest || String(jobs[0].created_at) > String(latest.created_at)) ? jobs[0] : null;
    const reason = !D.advisor ? 'loading' : !position ? 'missing' : !account.complete ? 'incomplete'
      : running ? 'running' : !API.writesEnabled || !API.isLive('advisor') ? 'unavailable' : null;
    return { saved, account, position, analyses, latest, running, failed, reason, canAnalyze: !reason };
  }

  function analysisCard(d, target) {
    const p = (d.policy || {}).proposal || {};
    const strength = (d.policy || {}).confidence ?? p.confidence;
    const meta = [C.t(d.account_scope_kind === 'all_personal' ? 'hd.allAccounts' : 'hd.thisAccount'),
      strength == null ? null : C.t('hd.strength', { n: strength })].filter(Boolean).join(' · ');
    const conditions = [['ai.entry', p.entry_logic], ['ai.exit', p.exit_logic]].filter(([, v]) => v);
    const risks = [...(p.primary_risks || []), ...(p.missing_information || [])];
    return '<article class="pf-card pf-analysis" data-od-id="analysis-' + h(d.run_id) + '">' +
      '<div class="pf-card-head"><span class="pf-verdict">' + h(d.action ? C.code(d.action) : C.t('ac.incomplete')) + '</span>' +
        '<span class="pf-fine">' + h(d.trade_date ? C.dateOnly(d.trade_date) : '') + '</span></div>' +
      '<p class="pf-fine pf-meta">' + h(meta) + '</p>' +
      (p.investment_thesis ? '<p class="pf-thesis pf-verbatim">' + h(p.investment_thesis) + '</p>' : '') +
      (conditions.length ? '<details class="pf-more"><summary>' + h(C.t('hd.conditions')) + '</summary><dl class="pf-pairs">' +
        conditions.map(([k, v]) => '<div><dt>' + h(C.t(k)) + '</dt><dd class="pf-verbatim">' + h(v) + '</dd></div>').join('') +
        '</dl></details>' : '') +
      (risks.length ? '<details class="pf-more"><summary>' + h(C.t('ai.risks')) + '</summary><ul class="pf-bullets">' +
        risks.map(r => '<li class="pf-verbatim">' + h(r) + '</li>').join('') + '</ul></details>' : '') +
      '<a class="pf-text-link" href="' + h(reportLink(d.run_id, target.account)) + '">' + h(C.t('ai.fullReport')) +
        '<span class="pf-chevron" aria-hidden="true"></span></a>' +
    '</article>';
  }

  function renderHolding(target) {
    const x = holdingState(target), pos = x.position;
    $('holding-symbol').textContent = target.ticker;
    $('holding-where').textContent = x.account ? C.t('pf.accountLabel', { label: x.account.label }) : C.t('pf.robinhood');
    $('holding-robinhood').href = 'https://robinhood.com/stocks/' + encodeURIComponent(target.ticker);

    /* The position. A figure the venue did not give stays a gap; the cost basis and the
       gain are only drawn when every figure they rest on is there. */
    C.tickValue($('holding-value'), money(pos?.market_value_usd));
    $('holding-value').hidden = !pos && !!D.advisor;
    const basis = pos && pos.average_cost != null ? pos.average_cost * pos.quantity : null;
    const gain = basis != null && pos.market_value_usd != null ? pos.market_value_usd - basis : null;
    C.setHTML($('holding-return'), gain == null ? '' :
      delta(gain, signedUsd(gain) + (pos.unrealized_pct == null ? '' : ' (' + signedPct(pos.unrealized_pct, 2) + ')'), C.t('hd.totalReturn')));
    $('holding-quote').textContent = pos?.price == null ? '' : C.t('hd.quote', { price: C.usd(pos.price),
      time: pos.quote_at ? C.dateTime(pos.quote_at) : C.t('rs.unknown') });
    $('holding-missing').hidden = !!pos || !D.advisor;
    $('holding-stats').hidden = !pos;
    C.setHTML($('holding-stats'), !pos ? '' :
      stat(C.t('hd.shares'), shares(pos.quantity)) + stat(C.t('ai.cost'), money(pos.average_cost)) +
      stat(C.t('hd.weight'), pos.weight_pct == null ? '—' : C.pct(pos.weight_pct)) + stat(C.t('hd.costBasis'), money(basis)));

    /* What is happening now, then the newest conclusion, then the way to ask for another. */
    const job = x.running || x.failed;
    C.setHTML($('holding-job'), !job ? '' : x.running
      ? '<div class="pf-job is-running"><span class="pf-pulse" aria-hidden="true"></span><div class="pf-job-copy"><strong>' +
          h(C.t('hd.running')) + '</strong><span>' + h(C.t('hd.runningMeta', { state: C.code(job.state),
          time: C.dateTime(job.created_at) })) + '</span></div>' +
          '<button class="pf-quiet" type="button" data-stop="' + h(job.run_key) + '"' +
          (stopping || !API.writesEnabled ? ' disabled' : '') + '>' + h(C.t('ai.cancel')) + '</button></div>'
      : '<div class="pf-job is-failed"><div class="pf-job-copy"><strong>' + h(C.t('hd.failed')) + '</strong><span class="pf-verbatim">' +
          h(job.error || C.code(job.state)) + '</span></div></div>');
    C.setHTML($('holding-analysis'), x.latest ? analysisCard(x.latest, target) : x.running ? ''
      : '<div class="pf-card pf-empty"><span class="pf-empty-mark" aria-hidden="true">' + C.ICON.assistant + '</span>' +
          '<p class="pf-notice-title">' + h(C.t('hd.notAnalyzed')) + '</p><p class="pf-fine">' + h(C.t('hd.notAnalyzedBody')) + '</p></div>');
    /* A first analysis is what this view is for, so it leads in green. Once there is a
       conclusion, asking again is the quieter choice, and while a run is in flight its own
       card above, with its Stop, is the only control. */
    const button = $('holding-analyze');
    button.textContent = C.t(analyzing ? 'hd.starting' : x.latest ? 'hd.analyzeAgain' : 'hd.analyze');
    button.disabled = analyzing || !x.canAnalyze;
    button.hidden = x.reason === 'missing' || !!x.running;
    button.classList.toggle('is-quiet', !!x.latest);
    $('holding-analyze-note').textContent = analyzeStatus || ({
      loading: '', missing: '', running: '', incomplete: C.t('hd.incomplete'),
      unavailable: C.t(window.CT_DEMO ? 'hd.demo' : 'hd.unavailable'),
    }[x.reason] ?? C.t('hd.analyzeNote'));

    const earlier = x.analyses.slice(1, 6);
    $('holding-history-block').hidden = !earlier.length;
    C.setHTML($('holding-history'), earlier.map(d =>
      '<a class="pf-row" href="' + h(reportLink(d.run_id, target.account)) + '">' +
        '<span class="pf-row-main"><span class="pf-row-title">' + h(d.trade_date ? C.dateOnly(d.trade_date) : C.t('rs.unknown')) + '</span>' +
        '<span class="pf-row-sub">' + h(C.t(d.account_scope_kind === 'all_personal' ? 'hd.allAccounts' : 'hd.thisAccount')) + '</span></span>' +
        '<span class="pf-verdict pf-verdict-soft">' + h(d.action ? C.code(d.action) : C.t('ac.incomplete')) + '</span>' +
        '<span class="pf-chevron" aria-hidden="true"></span></a>').join(''));
  }

  /* ── the strategy ledger ───────────────────────────────────────── */
  function renderLedger(liveShown) {
    const A = D.portfolio.accounts || {};
    const order = scope === 'demo' ? ['core', 'sandbox'] : ['micro_live'];
    const B = D.portfolio.buckets;
    const accountKey = scope === 'demo' ? 'conceptual' : 'micro_live';
    const total = A[accountKey]?.equity_usd ??
      order.filter((k) => B[k]).reduce((sum, k) => sum + B[k].equity_usd, 0);
    /* Every bucket withheld and a reason given: the money is there, it is the ledger's
       claim about itself that expired, so the figure is a gap and the reason is printed
       verbatim as the API worded it. */
    const failures = Object.entries(D.portfolio.errors || {})
      .filter(([key]) => scope === 'demo' ? key !== 'micro_live' : key === 'micro_live')
      .map(([, reason]) => reason);
    const withheld = failures.length > 0 && !order.some((key) => B[key]);
    /* Confirming is the remedy for a ledger nobody has confirmed lately, and for nothing else.
       When the market data is what is behind (error_codes, from the service), it clears on its
       own once prices arrive, and offering the button only invites stamping the same date. */
    const codes = Object.entries(D.portfolio.error_codes || {})
      .filter(([key]) => scope === 'demo' ? key !== 'micro_live' : key === 'micro_live').map(([, code]) => code);
    const pricesBehind = failures.length > 0 && codes.length > 0 &&
      codes.every((code) => code === 'ledger_ahead' || code === 'prices_unavailable');
    /* The card also carries the last check while it refused, before the ledger has expired:
       the figures are still on screen, and the reader can put the book right today. */
    const refusals = (check.ledgers || []).filter((l) => l.ledger === accountKey && l.error).map((l) => l.error);
    const notice = $('ledger-notice');
    notice.hidden = !failures.length && !refusals.length;
    $('ledger-held').hidden = !failures.length;
    $('ledger-held').textContent = C.t(!pricesBehind ? 'pf.ledgerHeld'
      : codes.includes('ledger_ahead') ? 'pf.heldPricesBehind' : 'pf.heldPricesMissing');
    $('ledger-check').textContent = check.date
      ? C.t('pf.lastCheck', { date: C.dateOnly(check.date),
                              result: C.t(check.confirmed ? 'pf.checkAgreed' : 'pf.checkRefused') })
      : '';
    $('ledger-reason').textContent = (failures.length ? failures : refusals).join(' · ');
    const row = refusedRow();
    $('ledger-diff').hidden = !row;
    if (row) {
      const more = row.cash_difference_usd > 0, amount = C.usd(Math.abs(row.cash_difference_usd));
      $('diff-text').textContent = C.t(more ? 'pf.cashMore' : 'pf.cashLess', { amount });
      const transfer = $('book-transfer'), income = $('book-income');
      transfer.textContent = armedKind === 'transfer' ? C.t('pf.tapAgain')
        : C.t('pf.bookTransfer', { kind: C.t(more ? 'cf.deposit' : 'cf.withdrawal'), amount });
      income.textContent = armedKind === 'income' ? C.t('pf.tapAgain')
        : C.t('pf.bookIncome', { kind: C.t(more ? 'cf.income' : 'cf.fee'), amount });
      transfer.disabled = income.disabled = booking || !API.writesEnabled;
    }
    $('book-status').textContent = bookStatus;
    $('confirm-ledger').disabled = confirming || !API.writesEnabled;
    $('confirm-ledger').hidden = pricesBehind;
    $('confirm-status').textContent = confirmStatus;

    /* the header is live data, not a value baked into the markup */
    const a = A[accountKey];
    C.tickValue($('pf-total'), withheld ? '—' : C.usd(total));
    $('pf-asof').textContent = scope === 'agentic' && withheld ? ''
      : C.t(scope === 'agentic' ? 'pf.ledgerPriced' : 'pf.totalAs', { d: C.dateOnly(D.portfolio.as_of) });
    $('strategy-badge').textContent = !a ? '' : a.is_real_money
      ? C.t('bucket.real', { venue: C.titleise(a.venue || '—') }) : C.t('bucket.conceptual');
    C.setHTML($('pf-result'), withheld || !a || a.investment_pnl_usd == null ? '' :
      delta(a.investment_pnl_usd, signedUsd(a.investment_pnl_usd) + (a.return_pct == null ? '' : ' (' + signedPct(a.return_pct, 2) + ')'),
        C.t('home.pnl')));

    /* ── the account: four figures up front, the rest of the ledger on request ── */
    C.setHTML($('account-cards'), Object.keys(A)
      .filter(key => key === accountKey)
      .map((key) => {
        const acct = A[key];
        const kv = (k, v) => '<div><dt>' + h(k) + '</dt><dd>' + h(v) + '</dd></div>';
        const cash = order.filter((k) => B[k]).reduce((sum, k) => sum + (B[k].cash_usd || 0), 0);
        return '<dl class="pf-stats pf-stats-flat" data-od-id="account-' + h(key) + '">' +
            stat(C.t('home.funded'), C.usd(acct.net_deposits_usd)) +
            stat(C.t('pf.cash'), order.some((k) => B[k]) ? C.usd(cash) : '—') +
            stat(C.t('acct.hwm'), acct.high_water_mark_usd == null ? '—' : C.usd(acct.high_water_mark_usd)) +
            stat(C.t('acct.drawdown'), C.pct(acct.drawdown_pct || 0) + ' · ' + C.code(acct.ladder_level)) +
          '</dl>' +
          '<details class="pf-more"><summary>' + h(C.t('pf.accountDetails')) + '</summary><dl class="pf-pairs pf-pairs-row">' +
            kv(C.t('acct.opening'), C.usd(acct.opening_allocation_usd)) +
            kv(C.t('acct.flows'), (acct.recorded_flows_usd >= 0 ? '+' : '') + C.usd(acct.recorded_flows_usd)) +
            kv(C.t('home.return'), acct.return_pct == null ? '—' : C.pct(acct.return_pct, 2)) +
            kv(C.t('pf.sourceLabel'), C.t('acct.src.' + key)) +
            kv(C.t('pf.ledgerDates'), C.t('acct.asOf', { d: C.dateOnly(acct.as_of), p: C.dateOnly(acct.priced_for) })) +
          '</dl></details>';
      }).join(''));

    /* ── positions, per bucket ─────────────────────────────────────── */
    const shown = order.filter((k) => B[k]);
    $('bucket-cards').hidden = liveShown;
    C.setHTML($('bucket-cards'), shown.map((key) => {
      const b = B[key];
      const rows = b.positions.map((pos) =>
        '<div class="pf-row">' +
          '<span class="pf-row-main"><span class="pf-row-title">' + h(pos.symbol) + '</span>' +
          '<span class="pf-row-sub">' + h(C.t('hd.sharesN', { n: shares(pos.quantity) }) + ' @ ' + C.usd(pos.price) +
            (pos.classification ? ' · ' + pos.classification : '')) + '</span></span>' +
          '<span class="pf-row-side"><span class="pf-row-value">' + C.usd(pos.market_value_usd) + '</span>' +
          delta(pos.unrealized_pct, signedPct(pos.unrealized_pct)) + '</span></div>').join('');
      /* A single sleeve is the whole bucket, which the cash and positions figures already say. */
      const sleeves = Object.keys(b.sleeves_pct || {}).length < 2 ? []
        : Object.entries(b.sleeves_pct).map(([k, v]) => C.titleise(k) + ' ' + C.pct(v));
      return '<article class="pf-card" data-od-id="portfolio-' + h(key) + '">' +
        '<div class="pf-card-head"><p class="pf-eyebrow">' + h(shown.length > 1 ? C.bucket(key) : C.t('pf.positionsTitle')) + '</p>' +
          '<span class="pf-fine">' + h(C.t('pf.positions', { n: b.position_count })) + '</span></div>' +
        (shown.length > 1 ? '<p class="pf-figure pf-figure-sm">' + C.usd(b.equity_usd) + '</p>' : '') +
        (b.positions.length ? '<div class="pf-rows pf-holdings">' + rows + '</div>'
          : '<div class="pf-empty pf-empty-inline" data-od-id="empty-' + h(key) + '"><p class="pf-notice-title">' + h(C.t('pf.noPositions')) +
              '</p><p class="pf-fine">' + h(C.t('pf.noPositionsBody')) + '</p></div>') +
        '<p class="pf-fine pf-card-foot">' + h([C.t('pf.cash') + ' ' + C.usd(b.cash_usd),
          C.t('pf.positionsValue') + ' ' + C.usd(b.positions_value_usd),
          C.t('bucket.ofAllocation', { v: C.usd(b.allocation_usd, 0) }), ...sleeves].join(' · ')) + '</p>' +
      '</article>';
    }).join(''));

    /* ── allocation, only where there is more than one bucket to divide between ── */
    const SHADE = { core: 'var(--fg)', sandbox: 'var(--muted)', micro_live: 'var(--fg)' };
    $('pf-allocation').hidden = shown.length < 2;
    C.setHTML($('alloc-bar'), shown.length < 2 ? '' : shown.map((k) =>
      '<span style="width:' + (total ? (B[k].equity_usd / total) * 100 : 0).toFixed(2) +
      '%;background:' + SHADE[k] + ';"></span>').join(''));
    C.setHTML($('alloc-legend'), shown.length < 2 ? '' : shown.map((k) =>
      '<div class="pf-row"><span class="pf-row-main"><span class="pf-row-title">' + h(C.bucket(k)) + '</span>' +
        '<span class="pf-row-sub">' + h(C.t('acct.' + B[k].account)) + '</span></span>' +
        '<span class="pf-row-side"><span class="pf-row-value">' + C.usd(B[k].equity_usd) + '</span>' +
        '<span class="pf-delta">' + h(C.t('pf.ofTotal', { p: C.pct(total ? (B[k].equity_usd / total) * 100 : 0) })) + '</span></span></div>'
    ).join(''));
  }

  /* ── the Agentic account as Robinhood has it now ─────────────────────
     The money in the real account is the venue's own figure, from the kept reading /micro-live
     serves: a minute old at most while someone is looking, and the same reading preflight gates
     on. It does not wait on the ledger. On 2026-09-23 a vendor's missing bar held the ledger's
     figures back for a day while the account itself read perfectly well. The ledger, which the
     risk rules run on, follows below with its own date. A figure the venue did not give stays
     a gap: no balance is ever drawn as $0, and no holding is valued at its cost. */
  function liveRow(p) {
    return '<div class="pf-row" data-od-id="live-' + h(p.symbol) + '">' +
      '<span class="pf-row-main"><span class="pf-row-title">' + h(p.symbol) + '</span>' +
        '<span class="pf-row-sub">' + h(C.t('hd.sharesN', { n: shares(p.quantity) }) + (p.price == null ? '' : ' @ ' + C.usd(p.price))) + '</span></span>' +
      '<span class="pf-row-side"><span class="pf-row-value">' + money(p.market_value_usd) + '</span>' +
        (p.unrealized_pct == null ? '<span class="pf-delta">—</span>' : delta(p.unrealized_pct, signedPct(p.unrealized_pct))) + '</span></div>';
  }
  function renderLive() {
    const agentic = scope === 'agentic';
    $('live-card').hidden = !agentic;
    if (!agentic) { $('live-holdings').hidden = true; return false; }
    // The demo has no venue to fall silent: its fixture is the reading, under the demo banner.
    const m = D.microLive || {}, acct = m.account || null, fresh = window.CT_DEMO || API.isLive('microLive');
    const failure = API.state?.resources?.microLive?.error;
    C.tickValue($('live-total'), acct ? C.usd(acct.equity) : '—');
    $('live-stamp').textContent = acct && m.checked_at ? C.t('pf.liveStamp', { time: C.dateTime(m.checked_at) }) : '';
    $('live-stamp').classList.toggle('is-stale', !fresh);
    C.setHTML($('live-stats'), !acct ? '' : stat(C.t('pf.cash'), C.usd(acct.cash)) + stat(C.t('pf.buyingPower'), C.usd(acct.buying_power)));
    const holdings = acct && Array.isArray(m.holdings) ? m.holdings : null;
    const note = !m.checked_at ? (failure ? C.t('pf.liveOff', { reason: failure }) : C.t('pf.liveLoading'))
      : m.connected === false ? C.t('pf.liveOff', { reason: m.connect_error || C.t('rs.unknown') })
      : !acct ? C.t('pf.liveUnread')
      : [!fresh ? C.t('pf.liveStale', { time: C.dateTime(m.checked_at) }) : '',
         holdings == null ? C.t('pf.liveHoldingsOff') : ''].filter(Boolean).join(' ');
    $('live-note').textContent = note;
    $('live-note').hidden = !note;
    $('live-holdings').hidden = holdings == null;
    $('live-count').textContent = holdings ? C.t('pf.positions', { n: holdings.length }) : '';
    C.setHTML($('live-rows'), !holdings ? '' : holdings.length
      ? '<div class="pf-rows pf-holdings">' + holdings.map(liveRow).join('') + '</div>'
      : '<p class="pf-fine pf-empty-line">' + h(C.t('pf.liveEmpty')) + '</p>');
    $('live-foot').textContent = !holdings || !holdings.length ? ''
      : [C.t('pf.liveHoldingsNote'), m.holdings_error].filter(Boolean).join(' · ');
    return holdings != null;
  }

  /* ── what the Agentic account may do without asking ───────────────────
     The two autonomy switches and the kill switch, on the account they govern. Read from
     /config/authority and /health, written through the calls Settings and System make. On asks
     for the password in a sheet and applies at once (POST /autonomy/switch: the owner's decision
     of 2026-09-23); off, pause and resume are one gesture that a second tap confirms, as the
     ledger's booking buttons are. A state that cannot be read offers no button: a guess drawn
     as a switch is worse than none. */
  const BLOCKS = [{ block: 'reduce', action: 'REDUCE', name: 'pfa.reduce' },
                  { block: 'open', action: 'BUY', name: 'pfa.open' }];
  let autoArmed = null, armTimer = null, autoBusy = false, autoStatus = '', sheetMode = null;
  const authorityRead = () => { const a = D.authority; return a && a.switches && a.actions ? a : null; };
  const blockName = (block) => C.t(BLOCKS.find((b) => b.block === block).name);
  const orAsIs = (key, raw) => { const text = C.t(key); return text === key ? raw : text; };

  function renderControls() {
    const card = $('agentic-controls');
    card.hidden = scope !== 'agentic';
    if (card.hidden) return;
    const a = authorityRead(), paused = C.isPaused(), known = window.CT_DEMO || API.isLive('health');
    const canWrite = !!a && API.writesEnabled && (window.CT_DEMO || API.isLive('authority')) && !autoBusy;
    // The brake never waits on the authority read: pausing is safe whatever else is unknown.
    const canBrake = API.writesEnabled && !autoBusy;
    const withdrawn = !!a && a.withdrawn?.tripped === true;
    const isOn = (block) => !!a && a.switches[block] === true;
    $('pfa-summary').textContent = !a ? C.t('pfa.unreadable')
      : paused ? C.t('pfa.sumPaused')
      : a.real_money_enabled === false ? C.t('pfa.sumNoMoney')
      : withdrawn ? C.t('pfa.sumWithdrawn', { reason: a.withdrawn.reason || '—' })
      : isOn('reduce') && isOn('open') ? C.t('pfa.sumBoth')
      : isOn('reduce') ? C.t('pfa.sumReduce') : isOn('open') ? C.t('pfa.sumOpen') : C.t('pfa.sumNone');

    const button = (key, label, kind, enabled = canWrite) => '<button class="pf-quiet ' + kind + '" type="button" data-pfa="' +
      key + '"' + (enabled ? '' : ' disabled') + '>' + h(label) + '</button>';
    const armed = (key, label, kind, enabled = canWrite) => autoArmed === key
      ? button(key, C.t('pf.tapAgain'), 'is-armed', enabled) : button(key, label, kind, enabled);
    const row = (id, title, sub, state, tone, action) =>
      '<div class="pf-row" data-od-id="pfa-' + id + '"><span class="pf-row-main"><span class="pf-row-title">' + h(title) +
        '</span><span class="pf-row-sub">' + h(sub) + '</span></span>' +
        '<span class="pf-state' + (tone ? ' ' + tone : '') + '">' + h(state) + '</span>' + action + '</div>';
    const rows = [];
    if (withdrawn) rows.push(row('restore', C.t('pfa.restoreTitle'), a.withdrawn.reason || '', '', '',
      button('restore', C.t('pfa.restore'), 'is-go')));
    if (a) for (const { block, action, name } of BLOCKS) {
      const limits = a.actions[action]?.limits || {}, on = isOn(block), n = (v) => v ?? '—';
      const sub = !on ? C.t(block === 'reduce' ? 'pfa.reduceOff' : 'pfa.openOff')
        : block === 'reduce' ? C.t('pfa.reduceOn', { pct: n(limits.min_remaining_pct_of_opened),
                                                     used: limits.used_today ?? 0, max: n(limits.max_positions_reduced_per_day) })
        : C.t('pfa.openOn', { conf: n(limits.min_confidence), pct: n(limits.max_pct_of_account),
                              used: limits.used_today ?? 0, max: n(limits.max_new_positions_per_day) });
      rows.push(row(block, C.t(name), sub,
        C.t(on && withdrawn ? 'pfa.held' : on ? 'pfa.on' : 'pfa.off'), on ? (withdrawn ? 'is-held' : 'is-on') : '',
        on ? armed('off:' + block, C.t('pfa.turnOff'), 'is-stop') : button('on:' + block, C.t('pfa.turnOn'), 'is-go')));
    }
    rows.push(row('orders', C.t('pfa.orders'),
      paused ? C.t('pfa.ordersPaused', { reason: (a && a.kill_switch?.reason) || '—' }) : C.t('pfa.ordersOn'),
      paused ? C.t('pfa.paused') : known ? C.t('pfa.normal') : '—', paused ? 'is-held' : known ? 'is-on' : '',
      paused ? armed('resume', C.t('pfa.resume'), 'is-go', canBrake) : armed('pause', C.t('pfa.pause'), 'is-stop', canBrake)));
    C.setHTML($('pfa-rows'), rows.join(''));

    const pending = !a ? null : (a.pending_changes || []).find((c) =>
      (c.paths || []).some((p) => String(p).startsWith('buckets.micro_live.autonomy.')));
    $('pfa-status').textContent = autoStatus
      || (window.CT_DEMO ? C.t('hd.demo') : !API.writesEnabled ? C.t('pfa.writesOff') : '')
      || (pending ? C.t('pfa.pending', { at: C.dateTime(pending.apply_after) }) : '');
  }

  $('pfa-rows').addEventListener('click', (event) => {
    const target = event.target.closest && event.target.closest('[data-pfa]');
    if (!target || target.disabled || autoBusy || !API.writesEnabled) return;
    const key = target.dataset.pfa, [verb, block] = key.split(':');
    if (verb !== 'pause' && verb !== 'resume' && !authorityRead()) return;
    if (verb === 'on') { openSheet({ kind: 'on', block }); return; }
    if (verb === 'restore') { openSheet({ kind: 'restore' }); return; }
    clearTimeout(armTimer);
    if (autoArmed !== key) {            // the first tap only arms; it falls back after a while
      autoArmed = key; autoStatus = '';
      armTimer = setTimeout(() => { autoArmed = null; renderControls(); }, 6000);
      renderControls();
      return;
    }
    autoArmed = null;
    act(verb, block);
  });

  async function act(verb, block) {
    autoBusy = true; autoStatus = ''; renderControls();
    try {
      const result = verb === 'off' ? await API.post('/autonomy/switch', { block, on: false, reason: 'switched off from Home' })
        : verb === 'pause' ? await API.post('/system/pause', { reason: 'paused from the Agentic account on Home' })
        : await API.post('/system/resume', {});
      if (result && result.blocked) { autoStatus = C.t('pfa.writesOff'); return; }
      if (verb !== 'off') {
        // The server's answer, not a local guess: the same rule the System page follows.
        C.setPaused(result && result.paused !== undefined ? !!result.paused : verb === 'pause');
        API.readOnce('health');
      }
      autoStatus = verb === 'off' ? C.t('pfa.doneOff', { name: blockName(block) })
        : C.t(verb === 'pause' ? 'pfa.donePause' : 'pfa.doneResume');
      await API.readOnce('authority');
    } catch (error) { autoStatus = C.t('pfa.failed', { error: error.message }); }
    finally { autoBusy = false; renderControls(); }
  }

  /* The sheet: turning a switch on, with its one number and the password; or restoring
     autonomy after the system stopped it, with the password. The password never outlives
     the sheet. */
  function openSheet(mode) {
    const a = authorityRead();
    if (!a) return;
    sheetMode = mode;
    const on = mode.kind === 'on', reduce = mode.block === 'reduce';
    const limits = on ? a.actions[reduce ? 'REDUCE' : 'BUY']?.limits || {} : {};
    const floor = a.limits?.confidence_floors?.autonomous_any ?? 80;
    $('pfa-sheet-title').textContent = on ? C.t('pfa.sheetOn', { name: blockName(mode.block) }) : C.t('pfa.restoreTitle');
    $('pfa-sheet-lead').textContent = !on ? C.t('pfa.restoreLead', { reason: a.withdrawn?.reason || '—' })
      : C.t(reduce ? 'pfa.leadReduce' : 'pfa.leadOpen');
    const items = !on ? [] : reduce
      ? [C.t('pfa.l.half'),
         limits.max_positions_reduced_per_day != null && C.t('pfa.l.reducePerDay', { n: limits.max_positions_reduced_per_day }),
         limits.min_confidence != null && C.t('pfa.l.confidence', { n: limits.min_confidence }),
         C.t('pfa.l.neverSell'), C.t('pfa.l.rules')]
      : [(limits.classes || []).length && C.t('pfa.l.classes', { list: limits.classes
           .map((cls) => orAsIs('pfa.class.' + cls, cls)).join(C.t('pfa.listJoin')) }),
         limits.max_pct_of_account != null && C.t('pfa.l.share', { n: limits.max_pct_of_account }),
         limits.max_new_positions_per_day != null && C.t('pfa.l.openPerDay', { n: limits.max_new_positions_per_day }),
         C.t('pfa.l.rules')];
    C.setHTML($('pfa-sheet-limits'), items.filter(Boolean).map((item) => '<li>' + h(item) + '</li>').join(''));
    $('pfa-value-field').hidden = !on;
    if (on) {
      const input = $('pfa-value');
      input.min = reduce ? 1 : floor; input.max = reduce ? 99 : 100;
      input.value = reduce ? (limits.min_remaining_pct_of_opened ?? 50) : Math.max(limits.min_confidence ?? floor, floor);
      $('pfa-value-label').textContent = C.t(reduce ? 'pfa.valueReduce' : 'pfa.valueOpen');
      $('pfa-value-unit').textContent = C.t(reduce ? 'pfa.unitReduce' : 'pfa.unitOpen');
    }
    // Buying is the switch that spends money on its own, so it says so first, on its own line.
    C.setHTML($('pfa-sheet-note'), !on ? '' : (reduce ? '' : '<strong>' + h(C.t('pfa.noteOpen')) + '</strong><br>') + h(C.t('pfa.note')));
    $('pfa-password').value = '';
    $('pfa-sheet-status').textContent = '';
    $('pfa-sheet-confirm').textContent = C.t(on ? 'pfa.turnOn' : 'pfa.restore');
    $('pfa-sheet-confirm').disabled = false;
    C.openSheet('pfa-sheet');
  }
  $('pfa-sheet').addEventListener('close', () => { $('pfa-password').value = ''; sheetMode = null; });
  $('pfa-sheet-cancel').addEventListener('click', () => C.closeSheet('pfa-sheet'));
  $('pfa-sheet-confirm').addEventListener('click', async () => {
    const mode = sheetMode, status = $('pfa-sheet-status'), password = $('pfa-password').value;
    if (!mode || autoBusy) return;
    let value = null;
    if (mode.kind === 'on') {
      const input = $('pfa-value'), min = Number(input.min), max = Number(input.max);
      value = Number(input.value);
      if (input.value === '' || !Number.isInteger(value) || value < min || value > max) {
        status.textContent = C.t('pfa.valueRange', { min, max }); return;
      }
    }
    if (!password) { status.textContent = C.t('pfa.needPassword'); return; }
    autoBusy = true; $('pfa-sheet-confirm').disabled = true; status.textContent = '';
    try {
      const result = mode.kind === 'on'
        ? await API.post('/autonomy/switch', { block: mode.block, on: true, value, password, reason: 'switched on from Home' })
        : await API.post('/autonomy/resume', { reason: 'restored from Home', password });
      if (result && result.blocked) { status.textContent = C.t('pfa.writesOff'); return; }
      autoStatus = mode.kind === 'on' ? C.t('pfa.doneOn', { name: blockName(mode.block) }) : C.t('pfa.doneRestore');
      C.closeSheet('pfa-sheet');
      await API.readOnce('authority');
    } catch (error) {
      status.textContent = C.t('pfa.failed', { error: error.message });
    } finally {
      autoBusy = false; $('pfa-sheet-confirm').disabled = false; renderControls();
    }
  });

  /* ── what needs you, then what is not normal ───────────────────────
     What needs you keeps the collector's priority. A partial or stale collector never implies
     an empty inbox, and the section goes only after a current, readable, empty answer. */
  function renderAttention() {
    const { data, items, known } = C.attentionState();
    const empty = known && !items.length;
    $('home-attention').hidden = empty;
    if (empty) {
      C.setHTML($('needs-you-slot'), '');
      return;
    }
    const card = item => {
      const href = C.taskLink(item.link);
      const kindKey = 'at.kind.' + item.kind;
      const label = C.t(kindKey) === kindKey ? C.t('at.item') : C.t(kindKey);
      const tone = ['critical', 'high', 'normal', 'info'].includes(item.severity) ? item.severity : 'normal';
      const icon = item.kind === 'proposal_waiting' ? C.ICON.approvals
        : item.kind === 'personal_task_done' ? C.ICON.check : C.ICON.info;
      const meta = [item.account_label, item.since ? C.dateTime(item.since) : null].filter(Boolean).join(' · ');
      /* The engine's sentence in this console's language where it wrote one in both. */
      const detail = C.said(item.detail_i18n) || item.detail;
      return '<article class="home-attention-item attention-' + tone + '" data-attention-id="' + h(item.id) + '">' +
        '<span class="attention-icon" aria-hidden="true">' + icon + '</span>' +
        '<div class="attention-copy"><div class="attention-kicker"><span>' + h(label) + '</span>' +
        '<span class="attention-state">' + C.t(item.requires_person ? 'at.person' : 'at.information') + '</span></div>' +
        '<h3>' + h(item.title) + '</h3>' +
        (detail ? '<p class="attention-detail">' + h(detail) + '</p>' : '') +
        '<div class="attention-footer">' + (meta ? '<p class="attention-meta">' + h(meta) + '</p>' : '') +
        (href ? '<a class="attention-action" href="' + h(href) + '">' + C.t('at.open') + C.CHEVRON + '</a>' : '') +
        '</div></div></article>';
    };
    const cards = items.map(card);
    const expanded = $('attention-rest')?.open;
    C.setHTML($('needs-you-slot'),
      '<div class="attention-heading"><h2 id="attention-heading">' + C.t('at.title') + '</h2>' +
      (cards.length ? '<span class="attention-count">' + h(C.t('at.count', { n: cards.length })) + '</span>' : '') + '</div>' +
      (!known ? '<div class="attention-unavailable" role="status"><span class="attention-icon" aria-hidden="true">' + C.ICON.info +
        '</span><div><p><strong>' + C.t('at.unknown') + '</strong> ' + C.t('at.partial') + '</p>' +
        (data?.unreadable?.length ? '<p class="attention-meta">' + h(data.unreadable.join('; ')) + '</p>' : '') + '</div></div>' : '') +
      (cards.length ? '<div class="attention-deck">' + cards[0] +
        (cards.length > 1 ? '<details id="attention-rest" class="attention-rest">' +
          '<summary><span class="attention-expand">' + C.t('at.more', { n: cards.length - 1 }) + '</span>' +
          '<span class="attention-collapse">' + C.t('at.collapse') + '</span>' + C.CHEVRON + '</summary>' +
          '<div class="attention-list">' + cards.slice(1).join('') + '</div></details>' : '') + '</div>' : ''));
    if ($('attention-rest')) $('attention-rest').open = !!expanded;
  }

  /* What is not normal. Each row names its own page. An unknown order is the attention
     collector's to report; one still working at the venue is shown here. The drawdown ladders
     each measure one account; the regime has a level too, and is not one of them. */
  const LADDERS = { micro_live: 'micro_live', total: 'conceptual', core: 'conceptual', sandbox: 'conceptual' };

  function renderConditions() {
    const rows = [];
    const status = C.systemStatus();
    if (status !== 'READY') {
      rows.push(C.listRow({ id: 'condition-status', href: 'system.html',
        title: C.code(status), sub: C.sysCopy(status) }));
    }
    const live = D.microLive || {};
    if (D.health.micro_live_enabled && live.preflight_ready === false) {
      rows.push(C.listRow({ id: 'condition-preflight', href: 'risk.html', title: C.t('home.preflight'),
        sub: C.t('home.blocking', { ids: (live.blocking || []).join(', ') || '—' }) }));
    }
    /* A ladder away from NORMAL. It is pushed once as it moves, but the attention list keeps
       only a halt, so this is where a lesser level stays in view. A halt is not repeated: the
       status row above and the attention list both carry it already. */
    Object.entries(LADDERS).forEach(([name, account]) => {
      const ladder = (D.risk || {})[name];
      if (!ladder || !ladder.level || ladder.level === 'NORMAL' || ladder.manual_reset_required ||
          !C.showsAccount(account)) return;
      rows.push(C.listRow({ id: 'condition-ladder-' + name, href: 'risk.html', title: C.t('home.ladder'),
        sub: C.t('home.ladderAt', { a: name === 'core' || name === 'sandbox' ? C.bucket(name) : C.t('acct.' + account),
          l: C.code(ladder.level), d: C.pct(ladder.drawdown_pct || 0, 2) }) }));
    });
    const working = ((D.orders || {}).tracked_orders || [])
      .filter((x) => ['SUBMITTED', 'PARTIALLY_FILLED'].includes(x.status));
    if (working.length) {
      rows.push(C.listRow({ id: 'condition-orders', href: 'activity.html', title: C.t('home.orders'),
        sub: C.t('home.ordersPending', { n: working.length }) }));
    }
    $('home-conditions').hidden = !rows.length;
    C.setHTML($('conditions-list'), rows.join(''));
  }
  document.addEventListener('ct:pause-changed', renderConditions);

  /* Everything below is re-run on every poll cycle. */
  function render() {
    const saved = D.advisor || {}, snapshot = saved.snapshot || {};
    const live = API.isLive('advisor');
    renderAttention();
    renderConditions();

    /* The switch between accounts: one lens that slides to the chosen one. */
    const views = [{ key: 'robinhood', title: C.t('pf.robinhood') }, { key: 'agentic', title: C.t('bucket.micro_live') }];
    if (C.SHOW_CONCEPTUAL) views.push({ key: 'demo', title: C.t('acct.conceptual') });
    const shell = $('pf-switch');
    shell.dataset.count = String(views.length);
    shell.dataset.index = String(Math.max(0, views.findIndex(view => view.key === scope)));
    $('portfolio-switch').setAttribute('aria-label', C.t('pf.switchLabel'));
    C.setHTML($('portfolio-switch'), views.map(view =>
      '<a class="pf-seg' + (scope === view.key ? ' is-selected' : '') +
      '" href="index.html?scope=' + view.key + (window.CT_DEMO ? '&amp;demo=1' : '') +
      '" data-od-id="portfolio-switch-' + view.key + '"' +
      (scope === view.key ? ' aria-current="page"' : '') + '>' + h(view.title) + '</a>').join(''));

    $('pf-overview').hidden = !!holding;
    $('holding-view').hidden = !holding;
    $('robinhood-view').hidden = scope !== 'robinhood';
    $('agentic-view').hidden = scope === 'robinhood';
    $('strategy-heading').textContent = C.t(scope === 'demo' ? 'acct.conceptual' : 'pf.ledgerTitle');
    $('ledger-card').classList.toggle('is-ledger', scope === 'agentic');

    /* Any active refresh affects this snapshot. Historical failures are shown
       only for the refresh requested from this page. */
    const mine = refreshJobs(saved).find(ACTIVE) || refreshJobs(saved).find((job) => job.run_key === askedFor);
    const busy = refreshing || !!(mine && ACTIVE(mine));
    const refresh = $('refresh-holdings');
    refresh.disabled = busy || !API.writesEnabled || !live;
    refresh.classList.toggle('is-busy', busy);
    $('broker-stamp').textContent =
      askError ? C.t('ux.refreshFailed', { error: askError })
      : busy ? C.t('ux.refreshing')
      : mine && mine.state !== 'COMPLETE' ? C.t('ux.refreshFailed', { error: mine.error || C.code(mine.state) })
      : snapshot.fetched_at ? C.t(saved.stale || !live ? 'ai.stale' : 'ai.fresh', { date: C.dateTime(snapshot.fetched_at) })
      : C.t('ai.empty');
    askRefresh(saved);
    /* The read's own notes are verbatim and one tap away. An account it could not read
       completely already says so on its card. */
    const notes = snapshot.errors || [];
    C.setHTML($('broker-accounts'), (snapshot.accounts || []).map(accountCard).join('') + (!notes.length ? '' :
      '<details class="pf-card pf-more pf-notes"><summary>' + h(C.t('pf.readNotes', { n: notes.length })) + '</summary>' +
        '<ul class="pf-bullets">' + notes.map(note => '<li class="pf-verbatim">' + h(note) + '</li>').join('') + '</ul></details>'));

    if (holding) renderHolding(holding);
    renderLedger(renderLive());
    renderControls();
  }

  /* Opening a holding starts at its top, and Back returns to the same place in the list.
     A holding reached from another screen has no list behind it, so its back control goes to
     the list instead of leaving the section. */
  let away = false, fromList = false, listScroll = 0;
  document.addEventListener('ct:leave', () => { away = true; });
  $('holding-back').addEventListener('click', (event) => {
    if (!fromList || !((window.history && window.history.state && window.history.state.ctDepth) > 0)) return;
    event.preventDefault();
    window.history.back();
  });
  document.addEventListener('ct:route', () => {
    const next = routeScope(), nextHolding = routeHolding(), entered = away;
    away = false;
    if (next !== scope) { armedKind = null; bookStatus = confirmStatus = ''; autoArmed = null; autoStatus = ''; }
    let scrollTo = null;
    if (nextHolding && !sameHolding(holding, nextHolding)) {
      if (!holding && !entered) listScroll = window.scrollY || 0;
      fromList = !holding && !entered;
      analyzeStatus = ''; scrollTo = 0;
      delete $('holding-value').dataset.prev;       /* another holding's figure, not a move */
    } else if (!nextHolding && holding) scrollTo = listScroll;
    scope = next; holding = nextHolding;
    render();
    if (scrollTo !== null && window.scrollTo) window.scrollTo(0, scrollTo);
  });
  C.live(['portfolio'], render, ['advisor', 'microLive', 'authority', 'attention', 'risk', 'orders']);
  loadCheck();
});
