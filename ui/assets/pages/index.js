window.Console.page('index', function (document) {
  const C = window.Console, D = C.D;
  C.mount('dashboard');

  const $ = (id) => document.getElementById(id);

  /* Today answers three things, in the order a person asks them: what needs me, what is not
     normal, and what research concluded. Whatever is normal and unchanging -- the market
     clock, a preflight that passes, an empty order book, a notice that has read the same for
     weeks -- has its own page and is not repeated here. The account's figures are Assets'
     alone: a card here repeated them and, reading only the ledger, went blank whenever the
     ledger was held back while Assets still showed the live balance. A drawdown that moves a
     ladder, a halt and an unconfirmed ledger each still reach this page, as attention or as
     a condition. */

  /* Runs on a hidden bucket stay out of Home, as they stay out of every headline. */
  /* Research about the owner's own money only: never a Demo book's, never a record bound to no
     account. */
  const visibleRuns = () => (D.research || []).filter((r) => C.showsBucket(r.bucket) &&
    !['demo', 'unspecified'].includes(r.scope));

  /* ── what is not normal ────────────────────────────────────────────
     Each row names its own page. An unknown order is the attention collector's to report;
     one still working at the venue is shown here. Health notices are counted, not repeated:
     System holds their text, and a notice that reads the same for weeks is not a to-do. */
  /* The drawdown ladders, each with the account it measures. The regime has a level too, and
     is not one of them. */
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
      const href = C.taskLink(item.link);
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
  const accountOf = (row) => row.account_label || (row.scope ? C.t('rs.scope.' + row.scope) : '');
  function reportLink(r) {
    const q = new URLSearchParams({ run: r.run_id });
    if (['agentic', 'demo', 'personal'].includes(r.scope)) q.set('scope', r.scope);
    if (r.scope === 'personal' && r.account_scope_kind === 'single' && r.account_id) q.set('account', r.account_id);
    if (r.scope === 'personal' && r.account_scope_kind === 'all_personal') q.set('include_all_personal', 'true');
    return 'lab.html?' + q;
  }

  function renderResearch() {
    /* Conversations, shortlists and refreshes share the queue but run no desks, so
       they have no office to open; strategy research and a personal analysis do. */
    const running = (D.researchJobs || []).filter((j) => (!j.task || j.task.kind === 'analysis') &&
      C.showsBucket(j.bucket) && !['demo', 'unspecified'].includes(j.scope) && ['QUEUED', 'RUNNING'].includes(j.state));
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
  C.live(['proposals'],
    function render() {
      $('today-date').textContent = C.dateOnly(new Date().toISOString());
      renderAttention();
      renderConditions();
      renderResearch();
    }, ['attention', 'microLive', 'risk', 'research', 'orders', 'researchJobs', 'schedule']);
});
