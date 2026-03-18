/* ─────────────────────────────────────────────────────────────────────────
 * Agentic Trading console — shared chrome and helpers.
 *
 * Reads window.CT: live pages receive same-origin API data; explicit demo
 * pages receive fixtures. The API layer tracks freshness independently.
 * ───────────────────────────────────────────────────────────────────── */

(function () {
  'use strict';

  const D = window.CT;

  /* ─── conceptual portfolio visibility ──────────────────────────────
     Core and Sandbox are a paper book: no venue behind them, nothing they
     produce can reach a broker, and their $3,000 is not the operator's
     money. With this off the console shows real capital only.

     Display only. The evaluation study, the scheduled core research and
     every ledger keep running exactly as before, and `ct-evaluate report`
     still reads the whole record. Set it to true to bring the paper book
     back into the interface.

     Approvals are deliberately not filtered. Nothing that is waiting on a
     human is ever hidden, whatever bucket it came from. */
  const SHOW_CONCEPTUAL = false;
  const CONCEPTUAL_BUCKETS = ['core', 'sandbox'];
  const showsBucket = (key) => SHOW_CONCEPTUAL || !CONCEPTUAL_BUCKETS.includes(key);
  const showsAccount = (key) => SHOW_CONCEPTUAL || key !== 'conceptual';

  /* The fixture is a snapshot taken at health.checked_at. Ages and countdowns
     tick forward from page load against that instant, so a freshness clock
     behaves the way it will against a live API without inventing a value. */
  const SNAPSHOT_AT = Date.parse(D.health.checked_at);
  const LOADED_AT = Date.now();
  /* Against fixtures, "now" is the snapshot instant plus elapsed page time so
     a countdown still behaves. Once real data is arriving, "now" is now. */
  const snapshotNow = () => !window.CT_DEMO
    ? Date.now()
    : SNAPSHOT_AT + (Date.now() - LOADED_AT);

  /* ─── formatting ──────────────────────────────────────────────────── */
  const usd = (n, digits) => (n < 0 ? '-' : '') + '$' + Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: digits === undefined ? 2 : digits,
    maximumFractionDigits: digits === undefined ? 2 : digits,
  });
  const pct = (n, digits) => n.toFixed(digits === undefined ? 1 : digits) + '%';
  const dateTime = (iso) => new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    hour12: false, timeZone: 'UTC',
  }) + ' UTC';
  const dateOnly = (iso) => new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  });
  const duration = (mins) => {
    const m = Math.max(0, Math.round(mins));
    if (m < 60) return m + 'm';
    const h = Math.floor(m / 60);
    if (h < 24) return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
    return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
  };
  const clock = (secs) => Math.floor(secs / 60) + ':' + String(Math.round(secs % 60)).padStart(2, '0');
  const titleise = (s) => String(s).replace(/_/g, ' ').toLowerCase()
    .replace(/(^|\s)\w/g, (c) => c.toUpperCase());

  /* ─── language ────────────────────────────────────────────────────
     Every string this console writes goes through t(). Anything the API
     returns is rendered verbatim — translating the engine's own words
     would be inventing them. */
  const t = (k, v) => window.I18N.t(k, v);
  /* readable gloss for an API status code; the code itself is never rewritten */
  const code = (x) => (x == null ? '' : (t('code.' + x) === 'code.' + x ? titleise(x) : t('code.' + x)));
  const sysCopy = (x) => t('sys.' + x);
  const psCopy = (x) => t('ps.' + x);
  const bucket = (k) => t('bucket.' + k);

  /* Ladders. The conceptual portfolios run five levels, Micro-Live three. */
  const LADDER_STEPS = {
    conceptual: ['NORMAL', 'WARNING', 'RISK_REDUCTION', 'HARD_HALT', 'EMERGENCY'],
    micro_live: ['NORMAL', 'WARNING', 'HALT'],
  };
  const LEVEL_TONE = {
    NORMAL: '', LOW_RISK: '',
    WARNING: 'warn', ELEVATED_RISK: 'warn', UNKNOWN: 'warn',
    RISK_REDUCTION: 'bad', HARD_HALT: 'bad', EMERGENCY: 'bad', HALT: 'bad', HIGH_RISK: 'bad',
  };

  /* ─── kill switch — POST /system/pause | /system/resume ───────────── */
  const isPaused = () => D.health.paused === true;
  const setPaused = (value) => {
    D.health.paused = value;
    document.dispatchEvent(new CustomEvent('ct:pause-changed', { detail: { paused: value } }));
  };
  const systemStatus = () => {
    if (!window.CT_DEMO && !CT_API.isLive('health')) return 'UNKNOWN';
    return isPaused() ? 'PAUSED' : D.health.status;
  };

  /* ─── proposal freshness, recomputed client-side between polls ────── */
  function freshnessNow(proposal) {
    const created = Date.parse(proposal.created_at);
    const age = Math.max(0, (snapshotNow() - created) / 60000);
    const ttl = proposal.freshness.ttl_minutes;
    const remaining = ttl - age;
    let state = 'VALID';
    if (remaining <= 0) state = 'EXPIRED';
    else if (remaining < ttl * 0.25) state = 'STALE';
    return { age, ttl, remaining, state, pctUsed: Math.min(100, (age / ttl) * 100) };
  }

  /* ─── icons — monoline, never emoji ───────────────────────────────── */
  const sv = (d, extra) => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="' + (extra || 1.7) + '" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true">' + d + '</svg>';
  const ICON = {
    check: sv('<path d="M4.5 12.5l5 5L19.5 7"/>', 2.6),
    cross: sv('<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>', 2.6),
    info: sv('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.6v.01"/>'),
    lock: sv('<rect x="4.5" y="10.5" width="15" height="10" rx="2.5"/><path d="M8 10.5V7a4 4 0 018 0v3.5"/>'),
    assistant: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C13.3 8.2 15.8 10.7 22 12c-6.2 1.3-8.7 3.8-10 10C10.7 15.8 8.2 13.3 2 12c6.2-1.3 8.7-3.8 10-10Z" fill="currentColor"/></svg>',
    home: sv('<path d="M4 10.5L12 4l8 6.5V19a1.5 1.5 0 01-1.5 1.5h-13A1.5 1.5 0 014 19z"/>'),
    portfolio: sv('<path d="M4 19.5V11M9.3 19.5V6M14.7 19.5v-5.5M20 19.5V8.5"/>', 2),
    approvals: sv('<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.3l2.4 2.4 4.6-5"/>'),
    research: sv('<circle cx="10.8" cy="10.8" r="6.3"/><path d="M15.4 15.4L20 20"/>'),
    lab: sv('<path d="M8 3v5l-4 9a2.5 2.5 0 002.3 3.5h11.4A2.5 2.5 0 0020 17l-4-9V3M7 12h10M7 3h10"/>'),
    more: sv('<circle cx="12" cy="8" r="3.25"/><path d="M5.5 20a6.5 6.5 0 0113 0"/>'),
    risk: sv('<path d="M12 3l7 3v5c0 4.7-2.8 8-7 10-4.2-2-7-5.3-7-10V6z"/><path d="M9.2 12.2l2 2 3.8-4"/>'),
    activity: sv('<path d="M7 5h13M7 12h13M7 19h13"/><circle cx="3.5" cy="5" r=".8" fill="currentColor" stroke="none"/><circle cx="3.5" cy="12" r=".8" fill="currentColor" stroke="none"/><circle cx="3.5" cy="19" r=".8" fill="currentColor" stroke="none"/>'),
    system: sv('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 00.34 1.88l.06.06-2.86 2.86-.06-.06A1.7 1.7 0 0015 19.4a1.7 1.7 0 00-1 .6 1.7 1.7 0 00-.4 1v.1H9.5V21a1.7 1.7 0 00-1.1-1.6 1.7 1.7 0 00-1.88.34l-.06.06-2.86-2.86.06-.06A1.7 1.7 0 004 15a1.7 1.7 0 00-1.6-1.1h-.1V10h.1A1.7 1.7 0 004 9a1.7 1.7 0 00-.34-1.88l-.06-.06L6.46 4.2l.06.06A1.7 1.7 0 008.4 4a1.7 1.7 0 001.1-1.6v-.1h4.1v.1A1.7 1.7 0 0015 4a1.7 1.7 0 001.88.34l.06-.06 2.86 2.86-.06.06A1.7 1.7 0 0019.4 9a1.7 1.7 0 001.6 1h.1v3.9H21a1.7 1.7 0 00-1.6 1.1z"/>'),
    language: sv('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/>'),
    session: sv('<path d="M10 5H5.5A1.5 1.5 0 004 6.5v11A1.5 1.5 0 005.5 19H10M14.5 8l4 4-4 4M8 12h10"/>'),
    personal: sv('<circle cx="12" cy="8" r="3"/><path d="M6 20a6 6 0 0112 0M18 5v4M16 7h4"/>'),
    notifications: sv('<path d="M6.5 10a5.5 5.5 0 0111 0c0 6 2.5 6 2.5 7H4c0-1 2.5-1 2.5-7M9.5 20h5"/>'),
    history: sv('<circle cx="12" cy="12" r="9"/><path d="M12 7.2V12l3.4 2"/>'),
    plus: sv('<path d="M12 5.5v13M5.5 12h13"/>', 2),
    back: sv('<path d="M15.5 19.5l-7-7.5 7-7.5"/>', 2.2),
    evaluation: sv('<path d="M4 19V9M10 19V5M16 19v-7M3 19h18M15.5 6.5L19 3l2 2"/>'),
    settings: sv('<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2" fill="var(--bg)"/><circle cx="15" cy="12" r="2" fill="var(--bg)"/><circle cx="8" cy="17" r="2" fill="var(--bg)"/>'),
  };

  const CHEVRON = '<svg class="chevron" viewBox="0 0 8 14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M1 1l6 6-6 6"/></svg>';

  /* ─── navigation ──────────────────────────────────────────────────── */
  const NAV = [
    { href: 'index.html',     key: 'dashboard', icon: 'home',      tab: true },
    { href: 'portfolio.html', key: 'portfolio', icon: 'portfolio', tab: true },
    { href: 'approvals.html', key: 'approvals', icon: 'approvals' },
    { href: 'advisor.html',   key: 'advisor',   icon: 'assistant' },
    /* Research is the tab, results first; the Lab is how one run was reached, opened from
       that run. The flask stays on the tab so the third place looks as it always has. */
    { href: 'research.html',  key: 'research',  icon: 'lab',       tab: true },
    { href: 'lab.html',       key: 'lab',       icon: 'lab' },
    { href: 'risk.html',      key: 'risk',     icon: 'risk' },
    { href: 'activity.html',  key: 'activity', icon: 'activity' },
    { href: 'system.html',    key: 'system',   icon: 'system' },
    { href: 'evaluation.html', key: 'evaluation', icon: 'evaluation' },
    { href: 'notifications.html', key: 'notifications', icon: 'notifications' },
    { href: 'profile.html',      key: 'profile',      icon: 'personal' },
    { href: 'settings.html',     key: 'settings',     icon: 'settings' },
  ];

  /* ─── the way back ────────────────────────────────────────────────
     These pages are reached by a link. The tab bar can only return you to a
     section root, never to the page you came from, so each one names its
     parent and the chrome renders a real back control for it. Pages with a
     tab are deliberately absent: a back button beside their own tab would
     be noise. The control is the arrow alone; its accessible name is "Back",
     not the parent's name, because in-app it returns to wherever you came from. */
  const PARENT = {
    approvals:  'index.html',
    lab:        'research.html',
    risk:       'more.html',
    activity:   'more.html',
    evaluation: 'more.html',
    system:     'more.html',
    notifications: 'more.html',
    profile:       'more.html',
    settings:      'more.html',
  };
  const openCount = () => D.proposals.filter((p) => p.status === 'WAITING_FOR_APPROVAL').length;

  function renderChrome(activeKey) {
    const open = openCount();
    const overflow = NAV.filter((n) => !n.tab);
    const section = activeKey === 'approvals' ? 'dashboard' : activeKey === 'lab' ? 'research' : activeKey;
    const inOverflow = overflow.some((n) => n.key === section);

    const tabs = NAV.filter((n) => n.tab).map((n) => {
      const dot = n.key === 'dashboard' ? '<span id="home-badge" class="tabdot"' + (open ? '' : ' hidden') + '></span>' : '';
      return '<a class="tabitem" href="' + n.href + '"' +
        (n.key === activeKey ? ' aria-current="page"' : n.key === section ? ' data-section-active="true"' : '') +
        ' data-od-id="tab-' + n.key + '">' + ICON[n.icon] + dot +
        '<span class="sr-only" data-nav-label="' + n.key + '">' + t('nav.' + n.key) + '</span></a>';
    }).join('') +
      '<a class="tabitem" href="more.html" id="tab-more"' +
      (activeKey === 'more' ? ' aria-current="page"' : (inOverflow ? ' data-section-active="true"' : '')) +
      ' data-od-id="tab-more">' + ICON.more +
      '<span class="sr-only" data-nav-label="more">' + t('nav.more') + '</span></a>';

    const parent = PARENT[activeKey];
    const head = parent
      ? '<a class="backlink" href="' + parent + '" id="back-link" data-od-id="back-link" ' +
          'data-i18n-aria="common.back" aria-label="' + escapeHtml(t('common.back')) + '">' + ICON.back + '</a>'
      : '<a class="brand" href="index.html" data-od-id="brand">' +
          '<img class="brand-mark" src="icons/mark.svg" width="30" height="30" alt="" /><span>Agentic Trading</span></a>';

    return '' +
      '<a class="skip-link" href="#content">' + t('common.skip') + '</a>' +
      '<header class="topnav" data-od-id="topnav"><div class="topnav-inner">' +
        head +
        '<span class="spacer"></span>' +
        (window.CT_DEMO ? '<span class="chip">' + t('rs.scope.demo') + '</span>' : '') +
        '<a class="assistant-launcher" id="assistant-launcher" href="advisor.html" data-od-id="assistant-launcher">' +
          ICON.assistant + '<span class="sr-only" data-nav-label="advisor">' + t('nav.advisor') + '</span></a>' +
      '</div></header>' +
      '<nav class="tabbar" aria-label="Sections"><div class="tabbar-inner"><span class="tab-indicator" aria-hidden="true"></span>' + tabs + '</div></nav>';
  }


  function paintNavStatus() {
    const badge = document.getElementById('home-badge');
    if (badge && badge.hidden !== !openCount()) badge.hidden = !openCount();
    const el = document.getElementById('nav-status');
    if (!el) return;
    /* Before you sign in the app has not asked the engine anything, so it has
       nothing to report. A red "Unknown" here claims a fault that is not
       happening — and repeats what the bar below already says. */
    const signedOut = !window.CT_DEMO && window.CT_API && !CT_API.authenticated;
    const pendingHealth = !window.CT_DEMO && CT_API.state.resources.health?.error === 'loading';
    if (el.hidden !== (signedOut || pendingHealth)) el.hidden = signedOut || pendingHealth;
    if (el.hidden) return;
    const s = systemStatus();
    const tone = s === 'READY' ? 'dot-ok' : (s === 'PAUSED' ? 'dot-warn' : 'dot-bad');
    setHTML(el, '<span class="dot ' + tone + '"></span><span>' + escapeHtml(code(s)) + '</span>');
    if (el.title !== sysCopy(s)) el.title = sysCopy(s);
  }

  function renderFoot() {
    return '<div id="page-loader" class="page-loader" hidden role="status" data-i18n-aria="common.loading" aria-label="' + escapeHtml(t('common.loading')) + '">' +
      '<div class="startup-loader" aria-hidden="true"><span class="lightsaber ls-left ls-green"></span>' +
      '<span class="lightsaber ls-right ls-red"></span>' +
      [1, 2, 3, 4, 5].map(n => '<span class="ls-particles ls-part-' + n + '"></span>').join('') + '</div></div>' +
      '<footer class="pagefoot" data-od-id="pagefoot"><div class="container">' +
      '<p class="body-sm" data-i18n="foot.local">' + t('foot.local') + '</p>' +
      '<p class="body-sm" style="margin-top:4px;" id="foot-stamp"></p>' +
      '</div></footer>';
  }

  function loading(show) {
    const loader = document.getElementById('page-loader');
    if (loader) loader.hidden = !show;
  }

  function paintFoot() {
    const el = document.getElementById('foot-stamp');
    if (!el) return;
    if (!window.CT_DEMO) {
      el.textContent = D.health.checked_at ? 'Health · ' + dateTime(D.health.checked_at) : '—';
      return;
    }
    const live = window.CT_API && CT_API.isLive();
    el.textContent = t(live ? 'foot.read' : 'foot.snapshot',
      { t: dateTime(D.health.checked_at), d: dateOnly(D.portfolio.as_of) });
  }

  /* Resource keys are internal names; the reader has a word for each. */
  const sourceName = (key) => {
    const label = t('ux.source.' + key);
    return label.startsWith('ux.source.') ? key : label;
  };

  /* What a failed read actually means, and the payload it came from.

     The engine reports freshness per book, so a stale portfolio arrives as
     {"conceptual": "...", "micro_live": "..."} carrying the same sentence
     twice. Printing that object at the reader was the bug: say the sentence
     once, and keep the payload for whoever is debugging. The engine's own
     words are never rewritten — translating them would be inventing them. */
  function explainError(raw) {
    if (!raw) return { text: t('ux.unavailable'), raw: null };
    if (raw === 'network offline') return { text: t('ux.offline'), raw: null };
    if (raw === 'loading') return { text: t('ux.connecting'), raw: null };
    if (raw === 'login required') return { text: t('ux.notSignedIn'), raw: null };
    let text = String(raw);
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {
        const unique = [...new Set(Object.values(parsed).map((v) => String(v).trim()))];
        if (unique.length) text = unique.join(' · ');
      }
    } catch (e) { /* not JSON — the string is already the message */ }
    return { text, raw: text === String(raw) ? null : String(raw) };
  }

  /* ─── connection banner — fixture data must never look live ───────── */
  function paintConnection() {
    const bar = document.getElementById('conn-bar');
    if (!bar || !window.CT_API) return;
    /* Called every second. It used to reset the bar and rebuild it on each call, so a page
       with nothing new still had its style recomputed once a second; now an unchanged bar is
       left alone. */
    let cls = 'conn-bar is-offline', hide = false;
    const apply = () => {
      if (bar.className !== cls) bar.className = cls;
      if (bar.hidden !== hide) bar.hidden = hide;
    };
    const zh = window.I18N.lang === 'zh';
    if (CT_API.state.demo) {
      setText(bar, zh ? '演示模式 · 全部为示例数据 · 所有操作关闭' : 'DEMO · Sample data only · All actions disabled');
      apply();
      return;
    }
    const rows = Object.entries(CT_API.state.resources).filter(([key]) => CT_API.SOURCES[key].path());
    const missing = rows.filter(([key]) => !CT_API.isLive(key));
    const loading = CT_API.state.authPending || (CT_API.authenticated && missing.length && missing.every(([, item]) => item.error === 'loading'));
    if (CT_API.state.authError && !CT_API.state.authPending) {
      setText(bar, t('ux.sessionUnavailable'));
    } else if (loading) {
      cls = 'conn-bar is-loading';
      setText(bar, t('common.loading'));
      hide = !!window.CT_NAV || document.getElementById('content')?.hidden === true;
    } else if (CT_API.state.authChecked && !CT_API.authenticated) {
      /* Being signed out is an ordinary state, not a fault — so this reads
         neutral, and carries the way in with this page as the return address. */
      cls = 'conn-bar';
      const here = window.location.pathname.split('/').pop() + window.location.search;
      setHTML(bar, '<span class="conn-msg">' + escapeHtml(t('ux.notSignedIn')) + '</span>' +
        '<a class="conn-signin" href="login.html?next=' + encodeURIComponent(here) + '">' +
        escapeHtml(t('ux.signIn')) + '</a>');
    } else if (!rows.length) {
      setText(bar, zh ? '正在读取真实数据…' : 'Loading account data…');
    } else if (missing.length || !CT_API.state.online) {
      /* Name what is affected and what it costs. The why belongs in the
         sources sheet, one tap away, not in the reader's face. */
      const names = missing.map(([key]) => sourceName(key)).join(' · ');
      const portfolioStale = missing.some(([key, item]) =>
        key === 'portfolio' && item.error && item.error.includes('older than'));
      const touchBtn = (portfolioStale && CT_API.authenticated)
        ? ' <button type="button" class="btn btn-sm btn-accent" data-action="confirm-ledger" style="margin-left:8px;padding:3px 10px;font-size:12px;vertical-align:middle;">' +
          escapeHtml(t('pf.confirmLedger')) + '</button>'
        : '';
      setHTML(bar, '<span class="conn-msg">' +
        escapeHtml(names ? t('ux.stale', { names }) : t('ux.offline')) + '</span>' + touchBtn);
    } else {
      cls = 'conn-bar';
      hide = true;
    }
    apply();
    setText(document.getElementById('sources-title'), t('ux.sources'));
    setHTML(document.getElementById('source-rows'), rows.map(([key, item]) => {
      const live = CT_API.isLive(key);
      const why = live ? null : explainError(item.error);
      const canTouch = !live && key === 'portfolio' && CT_API.authenticated &&
        item.error && item.error.includes('older than');
      return '<div class="source-row"><span class="dot ' + (live ? 'dot-ok' : 'dot-warn') + '"></span>' +
        '<div><strong>' + escapeHtml(sourceName(key)) + '</strong>' +
        '<p>' + escapeHtml(live ? t('ux.current') : t('ux.unavailable')) + '</p>' +
        (why && why.text ? '<p class="source-why">' + escapeHtml(why.text) + '</p>' : '') +
        (why && why.raw
          ? '<details class="source-raw"><summary>' + escapeHtml(t('ux.rawDetail')) +
            '</summary><pre>' + escapeHtml(why.raw) + '</pre></details>'
          : '') +
        (canTouch
          ? '<div style="margin-top:8px;"><button type="button" class="btn btn-sm btn-accent" data-action="confirm-ledger">' +
            escapeHtml(t('pf.confirmLedger')) + '</button></div>'
          : '') +
        '</div><time>' +
        escapeHtml(item.lastOkAt ? new Date(item.dataAt || item.lastOkAt).toLocaleTimeString() : '—') + '</time></div>';
    }).join(''));
  }

  /* Language lives in one place — the row on the Me page, which opens a
     picker and only commits on confirm. A second switch in the chrome made
     the same setting reachable two ways, and the chrome one changed the
     language on a single stray tap. */

  /* Offered where a stale ledger is reported. It used to post to /portfolio/touch, a route the
     API never had, so the one control shown in that state failed with "Not Found" every time.
     A stale ledger is renewed by a check that can refuse, never by writing a new date over it:
     this runs the same confirmation as the Portfolio page and ct-state confirm, and says what
     it refused. */
  async function confirmLedger(button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
      const report = await CT_API.post('/portfolio/confirm', { ledger: 'all' });
      if (report && report.blocked) return;
      if (report && !report.confirmed) {
        const refusals = [...(report.ledgers || []).filter(l => l.error).map(l => l.error),
                          ...(report.unsettled_orders || []).map(o => o.client_order_id)];
        alert(t('pf.confirmRefused', { error: refusals.join(' · ') || report.detail || '' }));
      }
      await CT_API.readOnce('portfolio');
    } catch (err) {
      alert(t('pf.confirmRefused', { error: err.message || err }));
    } finally {
      button.disabled = false;
    }
  }

  // Keep one physical glass lens across routes, so even interrupted transitions
  // continue from the currently painted position rather than replaying an entry.
  let syncTabIndicator = () => {};
  function wireTabIndicator() {
    const bar = document.querySelector('.tabbar');
    const track = bar.querySelector('.tabbar-inner');
    const lens = bar.querySelector('.tab-indicator');
    const tabs = [...bar.querySelectorAll('.tabitem')];
    let gesture = null, suppressClickUntil = 0, placed = false;
    const active = () => tabs.find(el => el.matches('[aria-current="page"], [data-section-active="true"]'));
    const position = (left, width) => {
      lens.style.width = width + 'px';
      lens.style.transform = 'translate3d(' + left + 'px, 0, 0)';
    };
    const clearPreview = () => tabs.forEach(el => el.removeAttribute('data-drag-active'));
    const finishGesture = () => {
      const previous = gesture;
      gesture = null;
      bar.classList.remove('is-dragging');
      clearPreview();
      if (previous && bar.hasPointerCapture(previous.id)) bar.releasePointerCapture(previous.id);
    };
    syncTabIndicator = () => {
      const target = active();
      if (!target || !track.getBoundingClientRect().width) return;
      finishGesture();
      if (!placed) lens.style.transition = 'none';
      position(target.offsetLeft, target.offsetWidth);
      if (!placed) {
        lens.getBoundingClientRect();
        lens.style.removeProperty('transition');
        placed = true;
      }
      lens.classList.add('is-ready');
    };
    const dragTo = clientX => {
      const width = gesture.width;
      const left = Math.max(0, Math.min(track.clientWidth - width,
        clientX - track.getBoundingClientRect().left - gesture.grab));
      position(left, width);
      const center = left + width / 2;
      gesture.target = tabs.reduce((nearest, el) =>
        Math.abs(el.offsetLeft + el.offsetWidth / 2 - center) <
        Math.abs(nearest.offsetLeft + nearest.offsetWidth / 2 - center) ? el : nearest);
      tabs.forEach(el => el.setAttribute('data-drag-active', String(el === gesture.target)));
    };
    bar.addEventListener('pointerdown', event => {
      if (!event.isPrimary || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
          !event.target.closest('.tabitem')) return;
      suppressClickUntil = 0;
      const rect = lens.getBoundingClientRect();
      gesture = {id: event.pointerId, x: event.clientX, y: event.clientY,
        width: rect.width, grab: event.clientX >= rect.left && event.clientX <= rect.right
          ? event.clientX - rect.left : rect.width / 2, dragging: false};
    });
    bar.addEventListener('pointermove', event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      const dx = Math.abs(event.clientX - gesture.x), dy = Math.abs(event.clientY - gesture.y);
      if (!gesture.dragging) {
        if (dy > dx && dy > 6) { finishGesture(); return; }
        if (dx < 6) return;
        gesture.dragging = true;
        bar.classList.add('is-dragging');
        bar.setPointerCapture(event.pointerId);
      }
      event.preventDefault();
      dragTo(event.clientX);
    });
    bar.addEventListener('pointerup', event => {
      if (!gesture || gesture.id !== event.pointerId) return;
      if (!gesture.dragging) { finishGesture(); return; }
      dragTo(event.clientX);
      const target = gesture.target;
      // The synthetic click uses the same routing path as taps and keyboards.
      // Suppress only the browser's subsequent pointer click, never that click.
      suppressClickUntil = performance.now() + 500;
      finishGesture();
      target.click();
      syncTabIndicator();
    });
    const cancel = () => {
      if (gesture?.dragging) suppressClickUntil = performance.now() + 500;
      finishGesture(); syncTabIndicator();
    };
    bar.addEventListener('pointercancel', cancel);
    // Touch starts with implicit capture on the icon. Its bubbling loss when
    // we transfer capture to the bar is not a cancelled drag.
    bar.addEventListener('lostpointercapture', event => {
      if (event.target === bar && gesture) cancel();
    });
    bar.addEventListener('dragstart', event => event.preventDefault());
    bar.addEventListener('click', event => {
      if (event.detail && performance.now() < suppressClickUntil) {
        event.preventDefault(); event.stopPropagation();
      }
    }, true);
    window.addEventListener('blur', cancel);
    if (window.ResizeObserver) new ResizeObserver(syncTabIndicator).observe(track);
    else window.addEventListener('resize', syncTabIndicator);
    syncTabIndicator();
  }

  let chromeMounted = false;
  function mount(activeKey) {
    if (chromeMounted) {
      const template = document.createElement('div');
      template.innerHTML = renderChrome(activeKey);
      document.querySelector('.topnav').replaceWith(template.querySelector('.topnav'));
      const nextTabs = template.querySelectorAll('.tabitem');
      document.querySelectorAll('.tabitem').forEach((tab, index) => {
        for (const attr of ['aria-current', 'data-section-active']) {
          const value = nextTabs[index].getAttribute(attr);
          if (value === null) tab.removeAttribute(attr);
          else tab.setAttribute(attr, value);
        }
      });
      syncTabIndicator();
      window.I18N.applyStatic();
      paintNavStatus(); paintFoot(); paintConnection();
      return;
    }
    chromeMounted = true;
    document.addEventListener('click', async event => {
      const retry = event.target.closest?.('[data-action="retry-load"]');
      if (retry) {
        if (retry.disabled) return;
        retry.disabled = true;
        try { await CT_API.refresh(); } finally { retry.disabled = false; }
        return;
      }
      const confirmButton = event.target.closest?.('[data-action="confirm-ledger"]');
      if (confirmButton) { await confirmLedger(confirmButton); return; }
      const open = event.target.closest?.('[data-open-sheet]');
      const close = event.target.closest?.('[data-close-sheet]');
      if (open) openSheet(open.dataset.openSheet);
      if (close) closeSheet(close.dataset.closeSheet);
      if (event.target.matches?.('dialog[data-sheet]')) closeSheet(event.target.id);
    });
    document.addEventListener('cancel', event => {
      if (event.target.matches?.('dialog[data-sheet]')) { event.preventDefault(); closeSheet(event.target.id); }
    }, true);
    document.body.insertAdjacentHTML('afterbegin', renderChrome(activeKey));
    wireTabIndicator();
    document.body.insertAdjacentHTML('beforeend', renderFoot());
    // Shared diagnostics stay mounted across routes, but only appear when
    // explicitly opened from System or the Assistant's account/tools sheet.
    document.body.insertAdjacentHTML('beforeend', '<dialog id="sources-sheet" class="glass-sheet" data-sheet aria-labelledby="sources-title">' +
      '<div class="sheet-grip"></div><div class="glass-sheet-head"><h2 id="sources-title" data-i18n="ux.sources">' + t('ux.sources') + '</h2>' +
      '<button class="circle-control" data-close-sheet="sources-sheet" data-i18n-aria="common.close" aria-label="' + t('common.close') + '">' + ICON.cross + '</button></div>' +
      '<div class="nav-status" id="nav-status" data-od-id="nav-status"></div>' +
      '<div class="conn-bar" id="conn-bar" hidden></div><div id="source-rows"></div></dialog>');
    window.I18N.applyStatic();
    if (window.CT_DEMO) document.addEventListener('click', event => {
      const link = event.target.closest('a[href]');
      if (!link) return;
      const url = new URL(link.href, window.location.href);
      if (url.origin === window.location.origin && /\.html$/.test(url.pathname) && !url.pathname.endsWith('/login.html')) {
        url.searchParams.set('demo', '1'); link.href = url.href;
      }
    });
    /* The href already points at the section root, which is the right answer
       for a cold open — a shared link, a new tab, a notification. When the
       page was reached from inside the app, the history entry that brought
       you here is a truer "back", so use that instead. */
    const backLink = document.getElementById('back-link');
    if (backLink) backLink.addEventListener('click', (event) => {
      if (window.CT_NAV || event.defaultPrevented || event.button || event.metaKey ||
          event.ctrlKey || event.shiftKey || event.altKey) return;
      let internal = false;
      try {
        internal = !!document.referrer &&
          new URL(document.referrer).origin === window.location.origin;
      } catch (e) { internal = false; }
      if (internal && window.history.length > 1) {
        event.preventDefault();
        window.history.back();
      }
    });
    /* A language change repaints the chrome and re-runs every screen render. */
    document.addEventListener('ct:lang', () => {
      document.querySelectorAll('[data-nav-label]').forEach((el) => {
        el.textContent = t('nav.' + el.getAttribute('data-nav-label'));
      });
      document.querySelectorAll('[data-nav-hint]').forEach((el) => {
        el.textContent = t('nav.hint.' + el.getAttribute('data-nav-hint'));
      });
      try { paintNavStatus(); paintFoot(); paintConnection(); } catch (e) {}
      document.dispatchEvent(new CustomEvent('ct:data', { detail: {} }));
    });
    document.addEventListener('ct:connection', () => { paintConnection(); paintNavStatus(); });
    try { paintNavStatus(); paintFoot(); } catch (e) { /* chrome only */ }
    document.addEventListener('ct:pause-changed', paintNavStatus);
  }

  /* ─── live(): bind a screen to the polling layer ──────────────────
     Pass the CT keys this screen reads and a render function. render()
     waits for initial live data (or explicit demo fixtures) and runs again
     after each resource update. */
  /* Unknown authentication is a loading state, never evidence of a logout.
     Keep a named page skeleton on screen until its own data is available. */
  function blankReason(keys) {
    if (window.CT_DEMO || !window.CT_API) return null;
    if (CT_API.state.authChecked && !CT_API.authenticated) {
      return { icon: ICON.lock, text: t('ux.signedOutBody') };
    }
    if (CT_API.state.authError && !CT_API.state.authPending) {
      return { icon: ICON.info, text: t('ux.sessionUnavailable'), retry: true };
    }
    const broken = Object.entries(CT_API.state.resources)
      .filter(([key, item]) => keys.includes(key) && CT_API.SOURCES[key].path() && !CT_API.isLive(key) &&
                               item.error && item.error !== 'loading');
    if (!broken.length || CT_API.state.authPending) return { loading: true, text: t('ux.loadingPage') };
    return { icon: ICON.info,
             text: t('ux.blockedBody', { names: broken.map(([key]) => sourceName(key)).join(' · ') }), retry: true };
  }

  function paintBlankState(show, keys) {
    const reason = show ? blankReason(keys) : null;
    let block = document.getElementById('blank-state');
    if (!reason) { if (block) block.remove(); return; }
    if (!block) {
      block = document.createElement('div');
      block.id = 'blank-state';
      document.getElementById('content').before(block);
    }
    const title = t(document.getElementById('content').dataset.i18nTitle || 'common.loading');
    block.className = reason.loading ? 'page-loading container' : 'blank-state';
    block.setAttribute('aria-busy', String(!!reason.loading));
    setHTML(block, reason.loading
      ? '<h1>' + escapeHtml(title) + '</h1><p role="status">' + escapeHtml(reason.text) + '</p>' +
        '<div class="page-skeleton" aria-hidden="true"><span></span><span></span><div></div><div></div></div>'
      : '<span class="blank-mark" aria-hidden="true">' + reason.icon + '</span>' +
        '<p role="status">' + escapeHtml(reason.text) + '</p>' +
        (reason.retry ? '<button class="btn btn-secondary btn-sm" data-action="retry-load">' + escapeHtml(t('ux.retryLoad')) + '</button>' : ''));
  }

  function live(keys, render, optionalKeys = []) {
    keys = [...new Set(keys)];
    const content = document.getElementById('content');
    const instant = !!window.CT_NAV;
    const wanted = [...new Set(['health', ...keys, ...optionalKeys, 'proposals'])];
    window.CT_NAV?.bind(wanted);
    let displayed = false;
    let heldControls = null;
    const draw = () => {
      if (instant && document.getElementById('content') !== content) return;
      const signedOut = CT_API.state.authChecked && !CT_API.authenticated;
      if (signedOut) displayed = false;
      // First render needs the verified owner (e.g. to restore the right chat draft).
      // A later connectivity problem should retain content already on screen.
      const loaded = CT_API.loaded(keys) && !signedOut &&
        (window.CT_DEMO || CT_API.authenticated || displayed || !keys.length);
      if (loaded) displayed = true;
      const reason = !loaded ? blankReason(keys) : null;
      const shell = instant && reason?.loading;
      loading(!!shell);
      content.hidden = !loaded && !shell;
      content.setAttribute('aria-busy', String(!loaded));
      // The actual page layout is visible before account data. No empty fixture is
      // rendered as a balance, and actions stay disabled until the first valid draw.
      if (shell && !heldControls) {
        heldControls = [...content.querySelectorAll('button, input, textarea, select')]
          .map(el => [el, el.disabled]);
        heldControls.forEach(([el]) => { el.disabled = true; });
      } else if (loaded && heldControls) {
        heldControls.forEach(([el, disabled]) => { el.disabled = disabled; });
        heldControls = null;
      }
      paintBlankState(!loaded && !shell, keys);
      let rendered = false;
      if (loaded) {
        try { render(); rendered = true; }
        catch (e) { console.error('[console] render failed', e); }
      }
      paintNavStatus(); paintFoot(); paintConnection();
      document.dispatchEvent(new CustomEvent('ct:page-status', { detail: {
        status: signedOut ? 'signedOut' : rendered ? 'ready' : loaded || reason?.retry ? 'error' : 'loading',
      } }));
    };
    draw();
    if (!window.CT_API) return;
    /* One draw per task, after the last data event in it: a tab switch sends two, and a poll
       that changes the queue sends its own and its detail's. The draw still comes before the
       next frame, so nothing is painted in between. */
    let queued = false;
    document.addEventListener('ct:data', () => {
      if (queued) return;
      queued = true;
      Promise.resolve().then(() => { queued = false; draw(); });
    });
    CT_API.start(wanted);
    paintConnection();
  }

  /* ─── tab groups ──────────────────────────────────────────────────── */
  function wireTabs(root) {
    (root || document).querySelectorAll('[data-tabs]').forEach((group) => {
      const tabs = group.querySelectorAll('[role="tab"]');

      /* One ink bar per group that slides to the selected tab, rather than a
         per-tab underline that pops in and out. */
      const ink = document.createElement('span');
      ink.className = 'tab-ink';
      group.appendChild(ink);
      group.classList.add('has-ink');

      const move = (animate) => {
        const on = group.querySelector('[role="tab"][aria-selected="true"]');
        if (!on) return;
        if (!animate) ink.style.transition = 'none';
        ink.style.width = on.offsetWidth + 'px';
        ink.style.transform = 'translateX(' + on.offsetLeft + 'px)';
        if (!animate) requestAnimationFrame(() => { ink.style.transition = ''; });
      };

      tabs.forEach((tab) => {
        tab.addEventListener('click', () => {
          tabs.forEach((t) => {
            const on = t === tab;
            t.setAttribute('aria-selected', on ? 'true' : 'false');
            const panel = document.getElementById(t.getAttribute('aria-controls'));
            if (panel) panel.hidden = !on;
          });
          move(true);
        });
      });

      move(false);
      window.addEventListener('resize', () => move(false));
    });
  }

  /* Write a number and, if it moved since the last poll, tick it once in the
     direction it moved. Silent on first paint. */
  function tickValue(el, text) {
    if (!el) return;
    const before = el.dataset.prev;
    el.textContent = text;
    if (before !== undefined && before !== text) {
      const a = parseFloat(String(before).replace(/[^0-9.-]/g, ''));
      const b = parseFloat(String(text).replace(/[^0-9.-]/g, ''));
      if (!isNaN(a) && !isNaN(b) && a !== b) {
        el.classList.remove('is-up', 'is-down');
        void el.offsetWidth;                     /* restart the animation */
        el.classList.add(b > a ? 'is-up' : 'is-down');
      }
    }
    el.dataset.prev = text;
  }

  /* ─── markdown, minimally ─────────────────────────────────────────
     The agent's reports are model output, so everything is escaped first and
     only then given structure. No HTML from the model ever reaches the DOM. */
  const escapeHtml = (t) => String(t)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  function md(text) {
    const lines = escapeHtml(text || '').split('\n');
    const out = [];
    let list = null;
    const closeList = () => { if (list) { out.push('</' + list + '>'); list = null; } };
    const inline = (t) => t
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');

    lines.forEach((raw) => {
      const line = raw.trimEnd();
      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      const ul = /^\s*[-*]\s+(.*)$/.exec(line);
      const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (h) { closeList(); out.push('<h4>' + inline(h[2]) + '</h4>'); return; }
      if (ul) {
        if (list !== 'ul') { closeList(); out.push('<ul>'); list = 'ul'; }
        out.push('<li>' + inline(ul[1]) + '</li>'); return;
      }
      if (ol) {
        if (list !== 'ol') { closeList(); out.push('<ol>'); list = 'ol'; }
        out.push('<li>' + inline(ol[1]) + '</li>'); return;
      }
      if (!line.trim()) { closeList(); return; }
      closeList();
      out.push('<p>' + inline(line) + '</p>');
    });
    closeList();
    return out.join('');
  }

  /* ─── shared markup helpers ───────────────────────────────────────── */
  function ladderMarkup(level, kind) {
    const steps = LADDER_STEPS[kind === 'micro_live' ? 'micro_live' : 'conceptual'];
    const idx = Math.max(0, steps.indexOf(level));
    const tone = LEVEL_TONE[level] || '';
    const bars = steps.map((_, i) =>
      '<span class="ladder-step' + (i <= idx ? ' is-on' + (tone ? ' is-on-' + tone : '') : '') + '"></span>'
    ).join('');
    return '<div class="ladder" role="img" aria-label="' +
      escapeHtml(t('rk.level', { i: idx + 1, n: steps.length, l: code(level) })) + '">' + bars + '</div>' +
      '<p class="ladder-caption">' +
        escapeHtml(t('rk.level', { i: idx + 1, n: steps.length, l: code(level) })) + '</p>';
  }

  /* one list row: title/sub on the left, value/note on the right,
     plus a chevron when the row navigates somewhere */
  function safeHref(href) {
    try { const url = new URL(href, window.location.href);
      return url.origin === window.location.origin && /^https?:$/.test(url.protocol) ? url.href : '#';
    } catch (e) { return '#'; }
  }

  function listRow(o) {
    const tag = o.href ? 'a' : 'div';
    const vals = (o.value ? '<span class="list-value">' + escapeHtml(o.value) + '</span>' : '') +
                 (o.note ? '<span class="list-note">' + escapeHtml(o.note) + '</span>' : '');
    return '<' + tag + ' class="list-row"' + (o.href ? ' href="' + escapeHtml(safeHref(o.href)) + '"' : '') +
      (o.trace ? ' data-trace="' + escapeHtml(o.trace) + '"' : '') +
      (o.id ? ' data-od-id="' + escapeHtml(o.id) + '"' : '') + '>' +
      '<span class="list-main">' +
        '<span class="list-title">' + escapeHtml(o.title) + '</span>' +
        (o.sub ? '<span class="list-sub">' + escapeHtml(o.sub) + '</span>' : '') +
      '</span>' +
      (vals || o.href
        ? '<span class="list-side">' +
            (vals ? '<span class="list-vals">' + vals + '</span>' : '') +
            (o.href ? CHEVRON : '') +
          '</span>'
        : '') +
    '</' + tag + '>';
  }

  function gateRow(g, kind) {
    const cls = ['is-pass', 'is-fail', 'is-warn'].includes(kind) ? kind : (g.passed ? 'is-pass' : 'is-fail');
    const mark = cls === 'is-pass' ? ICON.check : ICON.cross;
    return '<div class="gate ' + cls + '"' + (g.id ? ' data-od-id="gate-' + escapeHtml(String(g.id).toLowerCase()) + '"' : '') + '>' +
      '<span class="gate-mark">' + mark + '</span>' +
      '<div class="gate-body">' +
        '<div class="gate-name">' + (g.id ? '<span class="gate-id">' + escapeHtml(g.id) + '</span>' : '') + escapeHtml(g.name) + '</div>' +
        '<p class="gate-detail">' + escapeHtml(g.detail) + '</p>' +
      '</div></div>';
  }

  function noticeCard(message, index) {
    return '<article class="notice-card" data-od-id="warning-' + index + '"><span class="notice-icon">' + ICON.info +
      '</span><div><p class="notice-label">' + t('home.notices') + ' · ' + String(index + 1).padStart(2, '0') +
      '</p><p>' + escapeHtml(message) + '</p></div></article>';
  }
  const markup = new WeakMap();
  function setHTML(element, html) {
    if (!element || markup.get(element) === html) return;
    markup.set(element, html);
    element.innerHTML = html;
  }
  /* setHTML's twin for plain text. The key cannot collide with any markup setHTML records. */
  function setText(element, text) {
    const key = '\u0000' + text;
    if (!element || markup.get(element) === key) return;
    markup.set(element, key);
    element.textContent = text;
  }
  const sheetOrigins = new Map();
  function openSheet(id) {
    const sheet = document.getElementById(id);
    if (!sheet || sheet.open) return;
    sheetOrigins.set(id, document.activeElement);
    sheet.classList.remove('is-closing');
    sheet.showModal();
  }
  function closeSheet(id) {
    const sheet = document.getElementById(id);
    if (!sheet || !sheet.open || sheet.classList.contains('is-closing')) return;
    sheet.classList.add('is-closing');
    setTimeout(() => {
      sheet.close(); sheet.classList.remove('is-closing');
      sheetOrigins.get(id)?.focus?.(); sheetOrigins.delete(id);
    }, window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 0 : 150);
  }
  const LINK_PARAMS = {
    index: ['scope'], portfolio: ['scope', 'account', 'ticker'],
    advisor: ['scope', 'account', 'conversation', 'ticker'], research: ['scope', 'account', 'run', 'ticker', 'task_type', 'include_all_personal'],
    lab: ['run'], approvals: ['proposal'], activity: ['subject', 'kind', 'cursor'],
    settings: ['change', 'section'], risk: ['scope'], evaluation: ['scope'],
    system: [], profile: [], notifications: [], more: [],
  };
  function taskLink(link) {
    if (!link || !Object.hasOwn(LINK_PARAMS, link.page) || !link.params || typeof link.params !== 'object' || Array.isArray(link.params)) return null;
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(link.params)) {
      if (!LINK_PARAMS[link.page].includes(key) || typeof value !== 'string' || value.length > 256) return null;
      params.set(key, value);
    }
    if (window.CT_DEMO) params.set('demo', '1');
    return link.page + '.html' + (params.size ? '?' + params : '');
  }
  function attentionState() {
    const data = D.attention;
    const fresh = window.CT_DEMO || window.CT_API?.isLive('attention');
    return { data, items: Array.isArray(data?.items) ? data.items : [],
      known: !!(fresh && data?.readable === true && Array.isArray(data.items)) };
  }
  window.Console = {
    page: (name, init) => window.CT_NAV ? CT_NAV.register(name, init) : init(document),
    D, ICON, CHEVRON, NAV, PARENT, LEVEL_TONE, LADDER_STEPS,
    SHOW_CONCEPTUAL, showsBucket, showsAccount,
    t, code, sysCopy, psCopy, bucket,
    usd, pct, dateTime, dateOnly, duration, clock, titleise,
    snapshotNow, freshnessNow, ladderMarkup, listRow, gateRow, md, escapeHtml,
    isPaused, setPaused, systemStatus, paintNavStatus, openCount,
    setHTML, mount, wireTabs, live, loading, paintConnection, paintFoot, tickValue, openSheet, closeSheet, noticeCard,
    confirmLedger, taskLink, attentionState,
  };
})();
