window.Console.page('index', function (document) {
  const C = window.Console, D = C.D;
  C.mount('dashboard');

  const $ = (id) => document.getElementById(id);

  /* Today answers four things, in the order a person asks them: what needs me, what is not
     normal, what research concluded, and how the account stands. Whatever is normal and
     unchanging -- the market clock, a preflight that passes, an empty order book, a notice
     that has read the same for weeks -- has its own page and is not repeated here. */

  /* Runs on a hidden bucket stay out of Home, as they stay out of every headline. */
  const visibleRuns = () => (D.research || []).filter((r) => C.showsBucket(r.bucket));

  /* ── the account, against its own high-water mark ────────────────
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
  const assetsLink = (key) => 'portfolio.html?scope=' + (key === 'conceptual' ? 'demo' : 'agentic');

  function renderEquity() {
    const A = D.portfolio.accounts || {};
    const names = accountsInOrder();
    const lead = A[names[0]];
    if (!lead) {
      $('equity-label').textContent = C.t('home.noAccount');
      C.tickValue($('equity-value'), '—');
      $('equity-source').textContent = Object.values(D.portfolio.errors || {}).join('; ');
      $('hwm-foot').textContent = '';
      $('funding-kv').textContent = '';
      C.setHTML($('account-list'), '');
      return;
    }
    $('account-card').href = assetsLink(names[0]);
    $('account-card').className = (lead.is_real_money === false ? 'card-quiet' : 'card-real') + ' home-account';
    $('equity-label').textContent = C.t('home.equityOf', {
      a: C.t('acct.' + names[0]), d: C.dateOnly(lead.priced_for) });
    C.tickValue($('equity-value'), C.usd(lead.equity_usd));
    $('hwm-foot').textContent = C.t('home.hwmFoot',
      { v: C.usd(lead.high_water_mark_usd), d: C.pct(lead.drawdown_pct || 0), l: C.code(lead.ladder_level) });
    /* Money paid in is not money made. Both are shown so neither can be read
       as the other, and the return is stated against funding, not equity. */
    $('funding-kv').textContent = fundingLine(lead);
    $('equity-source').textContent = C.t('home.equitySource',
      { s: C.t('acct.src.' + names[0]), d: C.dateOnly(lead.as_of) });

    /* Any further account keeps its own equity, funding and high-water mark. */
    C.setHTML($('account-list'), names.slice(1).map((key) => {
      const a = A[key];
      return C.listRow({
        id: 'account-' + key,
        href: assetsLink(key),
        title: C.t('acct.' + key),
        sub: (a.is_real_money ? C.t('bucket.realShort') + ' · ' : '') + C.t('acct.src.' + key),
        value: C.usd(a.equity_usd),
        note: C.t('home.acctNote', {
          p: (a.investment_pnl_usd >= 0 ? '+' : '') + C.usd(a.investment_pnl_usd),
          d: C.pct(a.drawdown_pct || 0) }),
      });
    }).join(''));
  }

  function fundingLine(a) {
    return [
      C.t('home.funded') + ' ' + C.usd(a.net_deposits_usd),
      C.t('home.pnl') + ' ' + (a.investment_pnl_usd >= 0 ? '+' : '') + C.usd(a.investment_pnl_usd),
      C.t('home.return') + ' ' + (a.return_pct == null ? '—' : C.pct(a.return_pct, 2)),
    ].join(' · ');
  }

  /* ── what is not normal ────────────────────────────────────────────
     Each row names its own page. An unknown order is the attention collector's to report;
     one still working at the venue is shown here. Health notices are counted, not repeated:
     System holds their text, and a notice that reads the same for weeks is not a to-do. */
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
    const working = ((D.orders || {}).tracked_orders || [])
      .filter((x) => ['SUBMITTED', 'PARTIALLY_FILLED'].includes(x.status));
    if (working.length) {
      rows.push(C.listRow({ id: 'condition-orders', href: 'activity.html', title: C.t('home.orders'),
        sub: C.t('home.ordersPending', { n: working.length }) }));
    }
    const notices = D.health.warnings || [];
    if (notices.length) {
      rows.push(C.listRow({ id: 'condition-notices', href: 'system.html',
        title: C.t('at.systemNotice'), value: String(notices.length) }));
    }
    $('home-conditions').hidden = !rows.length;
    C.setHTML($('conditions-list'), rows.join(''));
  }

  /* ── what needs you ────────────────────────────────────────────────
     Attention retains server priority; a partial or stale collector never implies an
     empty inbox, and the section goes only after a current, readable, empty answer. */
  function renderAttention() {
    const { data, items, known } = C.attentionState();
    const h = C.escapeHtml;
    const empty = known && !items.length;
    $('home-attention').hidden = empty;
    if (empty) {
      C.setHTML($('needs-you-slot'), '');
      return;
    }
    const card = item => {
      // Until the Assistant has a result deep link, open the exact finished review here.
      const link = item.kind === 'personal_task_done' && item.link?.page === 'advisor' && item.subject
        ? { page: 'research', params: { run: item.subject } } : item.link;
      const href = C.taskLink(link);
      const kindKey = 'at.kind.' + item.kind;
      const label = C.t(kindKey) === kindKey ? C.t('at.item') : C.t(kindKey);
      const tone = ['critical', 'high', 'normal', 'info'].includes(item.severity) ? item.severity : 'normal';
      const icon = item.kind === 'proposal_waiting' ? C.ICON.approvals
        : item.kind === 'personal_task_done' ? C.ICON.check : C.ICON.info;
      const meta = [item.account_label, item.since ? C.dateTime(item.since) : null].filter(Boolean).join(' · ');
      return '<article class="home-attention-item attention-' + tone + '" data-attention-id="' + h(item.id) + '">' +
        '<span class="attention-icon" aria-hidden="true">' + icon + '</span>' +
        '<div class="attention-copy"><div class="attention-kicker"><span>' + h(label) + '</span>' +
        '<span class="attention-state">' + C.t(item.requires_person ? 'at.person' : 'at.information') + '</span></div>' +
        '<h3>' + h(item.title) + '</h3>' +
        (item.detail ? '<p class="attention-detail">' + h(item.detail) + '</p>' : '') +
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

  /* ── what research concluded ───────────────────────────────────────
     A study still running leads, and opens the office it is running in. A finished one
     opens its report. Each names the account it is about, because the same symbol can be
     studied for the Agentic account and for a personal one with different conclusions. */
  const accountOf = (row) => row.account_label || C.t('rs.scope.' + (row.scope || 'unspecified'));
  function reportLink(r) {
    const q = new URLSearchParams({ run: r.run_id });
    if (['agentic', 'demo', 'personal'].includes(r.scope)) q.set('scope', r.scope);
    if (r.scope === 'personal' && r.account_scope_kind === 'single' && r.account_id) q.set('account', r.account_id);
    if (r.scope === 'personal' && r.account_scope_kind === 'all_personal') q.set('include_all_personal', 'true');
    return 'research.html?' + q;
  }

  function renderResearch() {
    /* Assistant jobs share the queue but do not run the research graph, so they have no
       office to open. The presence of `task` is what separates them. */
    const running = (D.researchJobs || []).filter((j) => !j.task && C.showsBucket(j.bucket) &&
      ['QUEUED', 'RUNNING'].includes(j.state));
    const shown = new Set(running.map((j) => j.run_key));
    const rows = running.map((job) => C.listRow({
      id: 'research-live-' + job.run_key,
      href: 'lab.html?run=' + encodeURIComponent(job.run_key),
      title: C.t('agent.studying', { t: job.ticker || String(job.run_key).split('_')[1] || '—' }),
      sub: accountOf(job),
      note: C.t('home.viewProcess'),
    }));
    visibleRuns().filter((r) => !shown.has(r.run_id)).slice(0, 3).forEach((r) => rows.push(C.listRow({
      id: 'research-' + r.run_id,
      href: reportLink(r),
      title: (r.ticker || '—') + ' · ' + (r.final_action ? C.code(r.final_action) : C.t('ac.incomplete')),
      sub: [accountOf(r), C.dateOnly(r.trade_date)].join(' · '),
      value: r.confidence == null ? '' : String(r.confidence),
    })));
    C.setHTML($('research-list'), rows.length ? rows.join('')
      : '<p class="body-sm body-muted">' + C.t('agent.noRuns') + '</p>');
    const next = (D.schedule || {}).next_run_at;
    $('research-next').textContent = next ? C.t('lab.nextAt', { at: C.dateTime(next) }) : '';
  }

  document.addEventListener('ct:pause-changed', renderConditions);

  /* Poll only what this screen reads; render on first paint and every tick. */
  C.live(['portfolio', 'proposals'],
    function render() {
      $('today-date').textContent = C.dateOnly(new Date().toISOString());
      renderAttention();
      renderConditions();
      renderResearch();
      renderEquity();
    }, ['attention', 'microLive', 'research', 'orders', 'researchJobs', 'schedule']);
});
