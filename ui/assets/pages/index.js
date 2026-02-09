(function () {
  const C = window.Console, D = C.D;
  C.mount('dashboard');

  const $ = (id) => document.getElementById(id);

  /* Runs on a hidden bucket stay out of the headline and the Latest list. */
  const visibleRuns = () => (D.research || []).filter((r) => C.showsBucket(r.bucket));

  /* ── equity against the high-water mark ────────────────────────────
     One account at a time. The paper core/sandbox book and the real-money
     Micro-Live account keep separate ledgers and separate high-water marks,
     so a headline that added them together could not be compared against
     either mark: it would misstate both the assets and the drawdown. The
     lead figure is the primary account; the other is listed beneath it with
     its own numbers. */
  const PRIMARY = C.SHOW_CONCEPTUAL ? 'conceptual' : 'micro_live';

  function accountsInOrder() {
    const A = D.portfolio.accounts || {};
    return Object.keys(A).filter(C.showsAccount)
      .sort((a, b) => (a === PRIMARY ? -1 : b === PRIMARY ? 1 : a.localeCompare(b)));
  }

  function renderEquity() {
    const A = D.portfolio.accounts || {};
    const names = accountsInOrder();
    const lead = A[names[0]];
    if (!lead) {
      $('equity-label').textContent = C.t('home.noAccount');
      C.tickValue($('equity-value'), '—');
      $('equity-source').textContent = Object.values(D.portfolio.errors || {}).join('; ');
      C.setHTML($('funding-kv'), '');
      C.setHTML($('account-list'), '');
      $('home-drawdown').textContent = '';
      $('hwm-delta').textContent = '—';
      $('hwm-foot').textContent = '';
      $('hwm-fill').style.width = '0%';
      $('hwm-mark').style.left = '0%';
      $('hwm-track').setAttribute('aria-label', C.t('home.noAccount'));
      return;
    }
    $('home-drawdown').textContent = C.t('home.drawdown', { p: C.pct(lead.drawdown_pct || 0) });
    const equity = lead.equity_usd;
    const hwm = lead.high_water_mark_usd;

    $('equity-label').textContent = C.t('home.equityOf', {
      a: C.t('acct.' + names[0]), d: C.dateOnly(lead.priced_for) });
    C.tickValue($('equity-value'), C.usd(equity));
    $('equity-source').textContent = C.t('home.equitySource',
      { s: C.t('acct.src.' + names[0]), d: C.dateOnly(lead.as_of) });

    const delta = equity - hwm;
    const deltaPct = hwm ? (delta / hwm) * 100 : 0;
    const under = delta < 0;
    const el = $('hwm-delta');
    C.tickValue(el, C.t(under ? 'home.hwmBelow' : 'home.hwmAbove',
      { v: C.usd(Math.abs(delta)), p: Math.abs(deltaPct).toFixed(2) }));
    el.classList.add('hwm-delta', under ? 'neg' : 'pos');
    el.classList.toggle('pos', !under);
    el.classList.toggle('neg', under);

    /* one scale for both marks, with headroom so neither pins to the edge */
    const scale = Math.max(equity, hwm) * 1.1 || 1;
    const fill = $('hwm-fill');
    fill.style.width = ((equity / scale) * 100).toFixed(2) + '%';
    fill.className = 'hwm-fill' + (under ? ' is-under' : '');
    $('hwm-mark').style.left = ((hwm / scale) * 100).toFixed(2) + '%';
    $('hwm-track').setAttribute('aria-label',
      'Equity ' + C.usd(equity) + ' against a high-water mark of ' + C.usd(hwm));

    $('hwm-foot').textContent = C.t('home.hwmFoot',
      { v: C.usd(hwm), d: C.pct(lead.drawdown_pct || 0), l: C.code(lead.ladder_level) });

    /* Money paid in is not money made. Both are shown so neither can be read
       as the other, and the return is stated against funding, not equity. */
    C.setHTML($('funding-kv'), fundingRows(lead));

    /* Any further account keeps its own equity, funding and high-water mark. */
    C.setHTML($('account-list'), names.slice(1).map((key) => {
      const a = A[key];
      return C.listRow({
        id: 'account-' + key,
        href: 'portfolio.html',
        title: C.t('acct.' + key),
        sub: (a.is_real_money ? C.t('bucket.realShort') + ' · ' : '') + C.t('acct.src.' + key),
        value: C.usd(a.equity_usd),
        note: C.t('home.acctNote', {
          p: (a.investment_pnl_usd >= 0 ? '+' : '') + C.usd(a.investment_pnl_usd),
          d: C.pct(a.drawdown_pct || 0) }),
      });
    }).join(''));
  }

  function fundingRows(a) {
    const kv = (k, v) => '<div class="kv-row"><dt>' + C.escapeHtml(k) + '</dt><dd>' +
      C.escapeHtml(v) + '</dd></div>';
    return kv(C.t('home.funded'), C.usd(a.net_deposits_usd)) +
      kv(C.t('home.pnl'), (a.investment_pnl_usd >= 0 ? '+' : '') + C.usd(a.investment_pnl_usd)) +
      kv(C.t('home.return'), a.return_pct == null ? '—' : C.pct(a.return_pct, 2));
  }

  /* ── the agent's own state ───────────────────────────────────────
     RUNNING comes from /research/{run_key}; otherwise the queue and the
     last decision record say what it just did. The run_key format is
     {date}_{TICKER}_{hex} (service.py), so the ticker is readable from it. */
  function renderAgent() {
    const job = (D.researchJobs || []).find(j => ['QUEUED', 'RUNNING'].includes(j.state));
    if (!window.CT_DEMO && CT_API.activeRun !== (job?.run_key || null)) CT_API.watchRun(job?.run_key || null);
    const run = D.researchRun || job;
    $('agent-link').href = 'lab.html' + (run?.run_key ? '?run=' + encodeURIComponent(run.run_key) : '');
    const waiting = (D.proposals || []).filter((x) => x.status === 'WAITING_FOR_APPROVAL')[0];
    const last = visibleRuns()[0];
    const dot = $('agent-dot');
    let live = false, state, note;

    if (run && ['QUEUED', 'RUNNING'].includes(run.state)) {
      live = true;
      state = C.t('agent.studying', { t: String(run.run_key).split('_')[1] || '—' });
      note = C.t('agent.studyingNote');
    } else if (waiting) {
      state = C.t('agent.waiting');
      note = C.code(waiting.action) + ' ' + C.usd(waiting.notional_usd) + ' · ' +
             waiting.ticker + ' · ' + C.bucket(waiting.bucket);
    } else if (C.isPaused()) {
      state = C.t('agent.paused');
      note = C.t('agent.pausedNote');
    } else if (last) {
      state = C.t('agent.idle');
      note = C.t('agent.idleNote', {
        t: last.ticker, d: C.dateOnly(last.trade_date),
        a: last.final_action ? C.code(last.final_action) : C.t('home.noResult'),
        c: last.confidence });
    } else {
      state = C.t('agent.idle');
      note = C.t('agent.noRuns');
    }
    dot.className = 'agent-dot' + (live ? ' is-live' : '');
    $('agent-state').textContent = state;
    $('agent-note').textContent = note;
  }

  /* ── status chips ────────────────────────────────────────────────── */
  function renderChips() {
    const s = C.systemStatus();
    const normal = s === 'READY' && !(D.health.warnings || []).length && D.microLive?.preflight_ready !== false;
    const sys = $('chip-system');
    sys.textContent = normal ? C.t('home.allNormal') : C.code(s);
    sys.className = 'chip' + (s === 'READY' ? '' : ' chip-warn');
    sys.title = C.sysCopy(s);

    const open = D.health.market.is_open;
    $('chip-market').textContent = C.t(open ? 'home.marketOpen' : 'home.marketClosed');

    $('chip-market').hidden = normal;
    const money = $('chip-money');
    money.hidden = normal;
    const on = D.health.micro_live_enabled;
    money.textContent = C.t(on ? 'home.moneyOn' : 'home.moneyOff');
    money.className = 'chip' + (on ? ' chip-accent' : '');
  }

  /* ── the one thing that may need you ─────────────────────────────── */
  function renderProposal() {
    const p = (D.proposals || []).filter((x) => x.status === 'WAITING_FOR_APPROVAL')[0];
    if (!p) {
      const failures = Object.values(D.portfolio.errors || {});
      C.setHTML($('needs-you-slot'), failures.length
        ? '<a class="btn btn-secondary" href="portfolio.html#ledger-notice">' + C.t('home.reviewLedger') + '</a>'
        : (D.health.warnings || []).length ? ''
        : '<p class="body-sm body-muted" data-od-id="empty-queue">' + C.t('home.emptyQueue') + '</p>');
      return;
    }
    $('needs-you-slot').innerHTML =
      '<a class="card' + (p.is_real_money ? ' card-real' : '') + ' home-approval" href="approvals.html" data-od-id="proposal-card">' +
        '<span class="eyebrow">' + C.t('home.waitingOnYou') + ' · ' + C.openCount() + '</span>' +
        '<strong>' + C.escapeHtml(C.code(p.action)) + ' ' + C.usd(p.notional_usd) + ' · ' + C.escapeHtml(p.ticker) + '</strong>' +
        '<span class="body-sm">' + (p.is_real_money ? C.t('bucket.realShort') + ' · ' : '') + C.bucket(p.bucket) + '</span>' +
        '<span class="body-sm linkish">' + C.t('home.reviewCta') + '</span></a>';
  }

  /* ── latest ──────────────────────────────────────────────────────── */
  function renderLatest() {
    const rows = [];
    const run = visibleRuns()[0];
    if (run) {
      rows.push(C.listRow({
        id: 'latest-run', href: 'research.html',
        title: C.t('home.studied', { t: run.ticker,
          a: run.final_action ? C.code(run.final_action) : C.t('home.noResult') }),
        sub: C.t('home.studiedSub', { c: run.confidence }),
        value: C.dateOnly(run.trade_date),
        note: run.llm_calls + ' calls · ' + C.clock(run.elapsed_seconds),
      }));
    }
    const checks = (D.microLive && D.microLive.checks) || [];
    if (checks.length) {
      const blocking = D.microLive.blocking || [];
      rows.push(C.listRow({
        id: 'latest-preflight', href: 'risk.html',
        title: C.t('home.preflight'),
        sub: blocking.length ? C.t('home.blocking', { ids: blocking.join(', ') })
                             : C.t('home.allGreen'),
        value: checks.filter((c) => c.passed).length + ' / ' + checks.length,
        note: C.t('home.gatesGreen'),
      }));
    }
    const o = D.orders || { fills: [], open_orders: [] };
    const pending = (o.tracked_orders || []).filter(x => ['UNKNOWN', 'SUBMITTED', 'PARTIALLY_FILLED'].includes(x.status));
    rows.push(C.listRow({
      id: 'latest-orders', href: 'activity.html',
      title: C.t('home.orders'),
      sub: o.error ? o.error : pending.length ? pending.length + ' orders pending / 订单待核对或成交' : o.open_orders.length
        ? C.t('home.ordersOpen', { n: o.open_orders.length })
        : C.t('home.ordersNone'),
      value: String(o.fills.length),
      note: C.t('home.fills'),
    }));
    C.setHTML($('latest-list'), rows.join(''));
  }

  /* ── notices ─────────────────────────────────────────────────────── */
  function renderNotices() {
    const w = D.health.warnings || [];
    $('home-warnings').hidden = !w.length;
    $('warn-count').textContent = C.t('common.open', { n: w.length });
    C.setHTML($('warning-list'), w.length
      ? w.map(C.noticeCard).join('')
      : '<div class="banner"><span class="banner-mark">' + C.ICON.check +
        '</span><p>' + C.t('home.noNotices') + '</p></div>');
  }

  document.addEventListener('ct:pause-changed', () => { renderChips(); renderAgent(); });

  /* Poll only what this screen reads; render on first paint and every tick. */
  C.live(['portfolio', 'proposals'],
    function render() {
      renderAgent();
      renderEquity();
      renderChips();
      renderProposal();
      renderLatest();
      renderNotices();
    }, ['microLive', 'research', 'orders', 'researchJobs', 'researchRun']);
})();
