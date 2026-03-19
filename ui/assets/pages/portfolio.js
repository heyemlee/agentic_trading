window.Console.page('portfolio', function (document) {
  const C = window.Console, D = C.D, API = window.CT_API;
  C.mount('portfolio');
  const $ = id => document.getElementById(id);
  const h = C.escapeHtml;

  /* The URL selects one account view and keeps direct links stable. A Robinhood holding is
     one more level of the same address — portfolio.html?scope=robinhood&account=<id>&ticker=<symbol>
     — so it has a link of its own, Back returns to the list, and a reload lands on it again. */
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
  const link = params => 'portfolio.html?' + new URLSearchParams(params) + (window.CT_DEMO ? '&demo=1' : '');
  const reportLink = (runId, account) => 'lab.html?' + new URLSearchParams({ scope: 'personal', account,
    include_all_personal: 'true', run: runId }) + (window.CT_DEMO ? '&demo=1' : '');
  const stat = (label, value) => '<div class="pf-stat"><dt>' + h(label) + '</dt><dd>' + h(value) + '</dd></div>';
  const delta = (n, text, label) => n == null ? '' : '<span class="pf-delta' + tone(n) + '">' + h(text) + '</span>' +
    (label ? ' <span class="pf-delta-label">' + h(label) + '</span>' : '');

  /* ── one Robinhood account: its figure, then its holdings by value ─── */
  function holdingRow(account, p) {
    return '<a class="pf-row" href="' + h(link({ scope: 'robinhood', account: account.id, ticker: p.symbol })) +
      '" data-od-id="holding-' + h(p.symbol) + '">' +
      '<span class="pf-row-main"><span class="pf-row-title">' + h(p.symbol) + '</span>' +
        '<span class="pf-row-sub">' + h(C.t('hd.sharesN', { n: shares(p.quantity) })) + '</span></span>' +
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
     for the position, the recorded decisions for the analyses, the queue for a run in
     flight, the latest portfolio review for its line about this symbol. Records that never
     said which account they were about are left out, as the research page leaves them out
     of a single-account view; an all-accounts analysis of the same symbol is kept and says so. */
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
    const review = saved.latest_review;
    const covered = review && (review.account_scope_kind === 'single' ? review.account_id === target.account
      : review.account_scope_kind === 'all_personal' && (review.account_ids || []).includes(target.account));
    const reviewItem = covered ? (review.priorities || []).find(p => String(p.symbol).toUpperCase() === target.ticker) || null : null;
    const reason = !D.advisor ? 'loading' : !position ? 'missing' : !account.complete ? 'incomplete'
      : running ? 'running' : !API.writesEnabled || !API.isLive('advisor') ? 'unavailable' : null;
    return { saved, account, position, analyses, latest, running, failed, review: reviewItem && review, reviewItem,
             reason, canAnalyze: !reason };
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
    C.setHTML($('holding-analysis'), x.latest ? analysisCard(x.latest, target)
      : '<div class="pf-card pf-empty"><span class="pf-empty-mark" aria-hidden="true">' + C.ICON.assistant + '</span>' +
          '<p class="pf-notice-title">' + h(C.t('hd.notAnalyzed')) + '</p><p class="pf-fine">' + h(C.t('hd.notAnalyzedBody')) + '</p></div>');
    const button = $('holding-analyze');
    button.textContent = C.t(analyzing ? 'hd.starting' : x.running ? 'hd.analyzing' : x.latest ? 'hd.analyzeAgain' : 'hd.analyze');
    button.disabled = analyzing || !x.canAnalyze;
    button.hidden = x.reason === 'missing';
    $('holding-analyze-note').textContent = analyzeStatus || ({
      loading: '', missing: '', running: '', incomplete: C.t('hd.incomplete'),
      unavailable: C.t(window.CT_DEMO ? 'hd.demo' : 'hd.unavailable'),
    }[x.reason] ?? C.t('hd.analyzeNote'));

    /* The portfolio review's line about this symbol, only when the review covered this account. */
    $('holding-review-block').hidden = !x.reviewItem;
    C.setHTML($('holding-review'), !x.reviewItem ? '' :
      '<article class="pf-card pf-analysis"><div class="pf-card-head"><span class="pf-verdict pf-verdict-soft">' +
        h(C.t('hd.rec.' + x.reviewItem.recommendation) === 'hd.rec.' + x.reviewItem.recommendation
          ? C.titleise(x.reviewItem.recommendation) : C.t('hd.rec.' + x.reviewItem.recommendation)) + '</span>' +
        '<span class="pf-fine">' + h(x.review.analysed_at ? C.dateOnly(x.review.analysed_at) : '') + '</span></div>' +
        '<p class="pf-thesis pf-verbatim">' + h(x.reviewItem.rationale || '') + '</p>' +
        ((x.reviewItem.missing || []).length ? '<p class="pf-fine pf-aside pf-verbatim">' + h(C.t('rs.meta.missing') + ': ' +
          x.reviewItem.missing.join('; ')) + '</p>' : '') +
        (x.review.stale ? '<p class="pf-fine pf-aside">' + h(C.t('ai.reviewOld')) + '</p>' : '') +
        (x.review.run_key ? '<a class="pf-text-link" href="' + h(reportLink(x.review.run_key, target.account)) + '">' +
          h(C.t('ai.fullReport')) + '<span class="pf-chevron" aria-hidden="true"></span></a>' : '') +
      '</article>');

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

  /* Everything below is re-run on every poll cycle. */
  function render() {
    const saved = D.advisor || {}, snapshot = saved.snapshot || {};
    const live = API.isLive('advisor');

    /* The switch between accounts: one lens that slides to the chosen one. */
    const views = [{ key: 'robinhood', title: C.t('pf.robinhood') }, { key: 'agentic', title: C.t('bucket.micro_live') }];
    if (C.SHOW_CONCEPTUAL) views.push({ key: 'demo', title: C.t('acct.conceptual') });
    const shell = $('pf-switch');
    shell.dataset.count = String(views.length);
    shell.dataset.index = String(Math.max(0, views.findIndex(view => view.key === scope)));
    $('portfolio-switch').setAttribute('aria-label', C.t('pf.switchLabel'));
    C.setHTML($('portfolio-switch'), views.map(view =>
      '<a class="pf-seg' + (scope === view.key ? ' is-selected' : '') +
      '" href="portfolio.html?scope=' + view.key + (window.CT_DEMO ? '&amp;demo=1' : '') +
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
    if (next !== scope) { armedKind = null; bookStatus = confirmStatus = ''; }
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
  C.live(['portfolio'], render, ['advisor', 'microLive']);
  loadCheck();
});
