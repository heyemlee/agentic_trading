window.Console.page('portfolio', function (document) {
  const C = window.Console, D = C.D, API = window.CT_API;
  C.mount('portfolio');

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

  document.getElementById('refresh-holdings').addEventListener('click', () => askRefresh(D.advisor || {}, true));
  /* Research is started one holding at a time, from the holding's own row. A button that
     queued every holding at once was as many full runs as there were holdings, each thirteen
     model calls, all drawn from the same daily budget as the trading system's own research. */

  /* The strategy figures come from the ledgers, and a ledger fails closed once nothing
     has confirmed it for max_age_days. The API then answers with its buckets emptied and
     the reason in errors, which is why this page shows a gap rather than a zero — a zero
     would be a claim about money that is still there. Confirming is a check that can
     refuse, the same one ct-state confirm makes, so this button is a request to run it,
     not an override of it. */
  let confirming = false, confirmStatus = '';
  document.getElementById('confirm-ledger').addEventListener('click', async () => {
    if (confirming || !API.writesEnabled) return;
    confirming = true; confirmStatus = C.t('pf.confirming'); render();
    try {
      const report = await API.post('/portfolio/confirm', { ledger: 'all' });
      const refusals = [...(report.ledgers || []).filter(l => l.error).map(l => l.error),
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
  const refusedRow = () => (check.ledgers || []).find((l) => l.error && l.cash_difference_usd != null);
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
  document.getElementById('book-transfer').addEventListener('click', () => book('transfer'));
  document.getElementById('book-income').addEventListener('click', () => book('income'));

  /* Everything below is re-run on every poll cycle. */
  function render() {
    const saved = D.advisor || {}, snapshot = saved.snapshot || {};
    const live = API.isLive('advisor');
    /* Any active refresh affects this snapshot. Historical failures are shown
       only for the refresh requested from this page. */
    const mine = refreshJobs(saved).find(ACTIVE) || refreshJobs(saved).find((job) => job.run_key === askedFor);
    document.getElementById('refresh-holdings').disabled = refreshing || !!(mine && ACTIVE(mine)) || !API.writesEnabled || !live;
    document.getElementById('broker-stamp').textContent =
      askError ? C.t('ux.refreshFailed', { error: askError })
      : refreshing || mine && ACTIVE(mine) ? C.t('ux.refreshing')
      : mine && mine.state !== 'COMPLETE' ? C.t('ux.refreshFailed', { error: mine.error || C.code(mine.state) })
      : snapshot.fetched_at ? C.t(saved.stale || !live ? 'ai.stale' : 'ai.fresh', { date: C.dateTime(snapshot.fetched_at) })
      : C.t('ai.empty');
    askRefresh(saved);
    const money = value => value == null ? '—' : C.usd(value);
    C.setHTML(document.getElementById('broker-accounts'), (snapshot.accounts || []).map(a =>
      '<article class="account-glass"><div class="section-head"><h2>' + C.escapeHtml(a.label) + '</h2><span class="chip">' + C.t('ai.readOnly') + '</span></div>' +
      '<p class="figure">' + money(a.equity_usd) + '</p><p class="body-sm body-muted">' + C.t('ai.cash') + ' ' + money(a.cash_usd) + '</p>' +
      (!a.complete ? '<p class="body-sm">' + C.t('ux.incompleteAccount') + '</p>' : '') +
      (a.positions || []).map(p => C.listRow({ title: p.symbol,
        sub: C.t('ai.cost') + ' ' + money(p.average_cost) + ' · ' + p.quantity,
        value: money(p.market_value_usd), note: p.unrealized_pct == null ? '—' : C.pct(p.unrealized_pct),
        href: 'research.html?ticker=' + encodeURIComponent(p.symbol) })).join('') +
      (!(a.positions || []).length && a.complete ? '<p class="body-sm">' + C.t('ai.noPositions') + '</p>' : '') + '</article>'
    ).join('') + ((snapshot.errors || []).length ? '<p class="body-sm">' + C.escapeHtml(snapshot.errors.join(' · ')) + '</p>' : ''));

    const order = ['core', 'sandbox', 'micro_live'].filter(C.showsBucket);
    const B = D.portfolio.buckets;
    const A = D.portfolio.accounts || {};
    const combined = D.portfolio.combined || { equity_usd: 0 };
    /* The API's combined figure spans every bucket. With the paper book hidden
       the header has to add up only what is on the page, or it states a total
       the cards below cannot account for. */
    const total = C.SHOW_CONCEPTUAL ? combined.equity_usd
      : order.filter((k) => B[k]).reduce((sum, k) => sum + B[k].equity_usd, 0);
    /* Every bucket withheld and a reason given: the money is there, it is the ledger's
       claim about itself that expired, so the figure is a gap and the reason is printed
       verbatim as the API worded it. */
    const failures = Object.values(D.portfolio.errors || {});
    const withheld = failures.length > 0 && Object.keys(B).length === 0;
    /* The card also carries the last check while it refused, before the ledger has expired:
       the figures are still on screen, and the reader can put the book right today. */
    const refusals = (check.ledgers || []).filter((l) => l.error).map((l) => l.error);
    const notice = document.getElementById('ledger-notice');
    notice.hidden = !failures.length && !refusals.length;
    document.getElementById('ledger-held').hidden = !failures.length;
    document.getElementById('ledger-check').textContent = check.date
      ? C.t('pf.lastCheck', { date: C.dateOnly(check.date),
                              result: C.t(check.confirmed ? 'pf.checkAgreed' : 'pf.checkRefused') })
      : '';
    document.getElementById('ledger-reason').textContent = (failures.length ? failures : refusals).join(' · ');
    const row = refusedRow();
    document.getElementById('ledger-diff').hidden = !row;
    if (row) {
      const more = row.cash_difference_usd > 0, amount = C.usd(Math.abs(row.cash_difference_usd));
      document.getElementById('diff-text').textContent = C.t(more ? 'pf.cashMore' : 'pf.cashLess', { amount });
      const transfer = document.getElementById('book-transfer'), income = document.getElementById('book-income');
      transfer.textContent = armedKind === 'transfer' ? C.t('pf.tapAgain')
        : C.t('pf.bookTransfer', { kind: C.t(more ? 'cf.deposit' : 'cf.withdrawal'), amount });
      income.textContent = armedKind === 'income' ? C.t('pf.tapAgain')
        : C.t('pf.bookIncome', { kind: C.t(more ? 'cf.income' : 'cf.fee'), amount });
      transfer.disabled = income.disabled = booking || !API.writesEnabled;
    }
    document.getElementById('book-status').textContent = bookStatus;
    document.getElementById('confirm-ledger').disabled = confirming || !API.writesEnabled;
    document.getElementById('confirm-status').textContent = confirmStatus;
    /* Nothing conceptual is on screen, so the mixed-money warning would be
       describing a sum the reader cannot see. */
    const mixed = C.SHOW_CONCEPTUAL && combined.mixes_real_and_conceptual;

    const SHADE = { core: 'var(--fg)', sandbox: 'var(--muted)', micro_live: 'var(--green)' };

    /* the header is live data, not a value baked into the markup */
    C.tickValue(document.getElementById('pf-total'), withheld ? '—' : C.usd(total));
    /* A combined figure spanning a paper book and a funded account is a sum of
       two different kinds of money, so it says so rather than passing itself
       off as one portfolio. Each account below carries its own high-water mark
       and its own return, which is what any of these numbers can be compared
       against. */
    document.getElementById('pf-asof').textContent =
      C.t(mixed ? 'pf.totalMixed' : 'pf.totalAs',
          { d: C.dateOnly(D.portfolio.as_of) });

    /* ── per account: equity, funding, result, drawdown ──────────────── */
    C.setHTML(document.getElementById('account-cards'), Object.keys(A)
      .filter(C.showsAccount)
      .sort((a, b) => (a === 'conceptual' ? -1 : b === 'conceptual' ? 1 : a.localeCompare(b)))
      .map((key) => {
        const a = A[key];
        const kv = (k, v) => '<div class="kv-row"><dt>' + C.escapeHtml(k) + '</dt><dd>' +
          C.escapeHtml(v) + '</dd></div>';
        return '<div class="card' + (a.is_real_money ? ' card-real' : '') +
            '" data-od-id="account-' + C.escapeHtml(key) + '">' +
          '<div class="section-head">' +
            '<span class="chip' + (a.is_real_money ? ' chip-accent' : '') + '">' +
              C.escapeHtml(a.is_real_money ? C.t('bucket.real', { venue: C.titleise(a.venue || '—') })
                                           : C.t('bucket.conceptual')) + '</span>' +
            '<span class="body-sm body-muted">' + C.escapeHtml(C.t('acct.src.' + key)) + '</span>' +
          '</div>' +
          '<h2>' + C.escapeHtml(C.t('acct.' + key)) + '</h2>' +
          '<p class="figure" style="margin-top:6px;">' + C.usd(a.equity_usd) + '</p>' +
          '<p class="body-sm body-muted" style="margin-top:4px;">' +
            C.escapeHtml(C.t('acct.asOf', { d: C.dateOnly(a.as_of), p: C.dateOnly(a.priced_for) })) + '</p>' +
          '<dl class="kv" style="margin-top:16px;">' +
            kv(C.t('home.funded'), C.usd(a.net_deposits_usd)) +
            kv(C.t('acct.opening'), C.usd(a.opening_allocation_usd)) +
            kv(C.t('acct.flows'), (a.recorded_flows_usd >= 0 ? '+' : '') + C.usd(a.recorded_flows_usd)) +
            kv(C.t('home.pnl'), (a.investment_pnl_usd >= 0 ? '+' : '') + C.usd(a.investment_pnl_usd)) +
            kv(C.t('home.return'), a.return_pct == null ? '—' : C.pct(a.return_pct, 2)) +
            kv(C.t('acct.hwm'), a.high_water_mark_usd == null ? '—' : C.usd(a.high_water_mark_usd)) +
            kv(C.t('acct.drawdown'), C.pct(a.drawdown_pct || 0) + ' · ' + C.code(a.ladder_level)) +
          '</dl></div>';
      }).join(''));

    /* ── allocation ──────────────────────────────────────────────────── */
    C.setHTML(document.getElementById('alloc-bar'), order.filter((k) => B[k]).map((k) =>
      '<span style="width:' + (total ? (B[k].equity_usd / total) * 100 : 0).toFixed(2) +
      '%;background:' + SHADE[k] + ';"></span>').join(''));
    C.setHTML(document.getElementById('alloc-legend'), order.filter((k) => B[k]).map((k) =>
      C.listRow({
        title: C.bucket(k),
        sub: C.t('acct.' + B[k].account),
        value: C.usd(B[k].equity_usd),
        note: C.t('pf.ofTotal', { p: C.pct(total ? (B[k].equity_usd / total) * 100 : 0) }),
      })).join(''));

    /* ── bucket cards ────────────────────────────────────────────────── */
    C.setHTML(document.getElementById('bucket-cards'), order.filter((k) => B[k]).map((key) => {
      const b = B[key];
      const real = b.is_real_money;
      const positions = b.positions.length
        ? '<div class="list list-inset" style="margin-top:16px;">' + b.positions.map((pos) =>
            C.listRow({
              title: pos.symbol, sub: pos.classification || '',
              value: C.usd(pos.market_value),
              note: pos.quantity + ' @ ' + C.usd(pos.price) + ' · ' + C.pct(pos.unrealized_pct),
            })).join('') + '</div>'
        : '<div class="empty" style="margin-top:16px;" data-od-id="empty-' + key + '">' +
            '<p class="empty-title">' + C.t('pf.noPositions') + '</p>' +
            '<p>' + C.t('pf.noPositionsBody') + '</p></div>';

      return '<div class="card' + (real ? ' card-real' : '') + '" data-od-id="portfolio-' + key + '">' +
        '<div class="section-head">' +
          '<span class="chip' + (real ? ' chip-accent' : '') + '">' +
            (real ? C.escapeHtml(C.t('bucket.real', { venue: C.titleise(b.venue || '—') }))
                  : C.t('bucket.conceptual')) + '</span>' +
          '<span class="body-sm body-muted">' +
            C.escapeHtml(C.t('pf.positions', { n: b.position_count })) + '</span>' +
        '</div>' +
        '<h2>' + C.bucket(key) + '</h2>' +
        '<p class="figure" style="margin-top:6px;">' + C.usd(b.equity_usd) + '</p>' +
        '<p class="body-sm body-muted" style="margin-top:4px;">' +
          C.t('bucket.ofAllocation', { v: C.usd(b.allocation_usd, 0) }) + '</p>' +
        '<dl class="kv" style="margin-top:16px;">' +
          '<div class="kv-row"><dt>' + C.t('pf.cash') + '</dt><dd>' + C.usd(b.cash_usd) + '</dd></div>' +
          '<div class="kv-row"><dt>' + C.t('pf.positionsValue') + '</dt><dd>' + C.usd(b.positions_value_usd) + '</dd></div>' +
          Object.entries(b.sleeves_pct).map(([k, v]) =>
            '<div class="kv-row"><dt>' + C.escapeHtml(C.t('pf.sleeve', { k: C.titleise(k) })) + '</dt><dd>' + C.pct(v) + '</dd></div>').join('') +
        '</dl>' + positions +
      '</div>';
    }).join(''));
  }

  C.live(['portfolio'], render, ['advisor']);
  loadCheck();
});
