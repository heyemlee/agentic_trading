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
  const mlCopy = (x) => t('ml.' + x);
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
    home: sv('<path d="M4 10.5L12 4l8 6.5V19a1.5 1.5 0 01-1.5 1.5h-13A1.5 1.5 0 014 19z"/>'),
    portfolio: sv('<path d="M4 19.5V11M9.3 19.5V6M14.7 19.5v-5.5M20 19.5V8.5"/>', 2),
    approvals: sv('<circle cx="12" cy="12" r="8.5"/><path d="M8.5 12.3l2.4 2.4 4.6-5"/>'),
    research: sv('<circle cx="10.8" cy="10.8" r="6.3"/><path d="M15.4 15.4L20 20"/>'),
    more: sv('<circle cx="5.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>' +
             '<circle cx="12" cy="12" r="1.3" fill="currentColor" stroke="none"/>' +
             '<circle cx="18.5" cy="12" r="1.3" fill="currentColor" stroke="none"/>'),
  };

  const CHEVRON = '<svg class="chevron" viewBox="0 0 8 14" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M1 1l6 6-6 6"/></svg>';

  /* ─── navigation ──────────────────────────────────────────────────── */
  const NAV = [
    { href: 'index.html',     key: 'dashboard', icon: 'home',      tab: true },
    { href: 'portfolio.html', key: 'portfolio', icon: 'portfolio', tab: true },
    { href: 'approvals.html', key: 'approvals', icon: 'approvals' },
    { href: 'advisor.html',   key: 'advisor',   icon: 'research',  tab: true },
    { href: 'research.html',  key: 'research',  icon: 'research' },
    { href: 'risk.html',      key: 'risk' },
    { href: 'activity.html',  key: 'activity' },
    { href: 'system.html',    key: 'system' },
  ];
  const openCount = () => D.proposals.filter((p) => p.status === 'WAITING_FOR_APPROVAL').length;

  function renderChrome(activeKey) {
    const open = openCount();
    const overflow = NAV.filter((n) => !n.tab);
    const inOverflow = overflow.some((n) => n.key === activeKey);

    const tabs = NAV.filter((n) => n.tab).map((n) => {
      const dot = n.key === 'approvals' && open ? '<span class="tabdot"></span>' : '';
      return '<a class="tabitem" href="' + n.href + '"' +
        (n.key === activeKey ? ' aria-current="page"' : '') +
        ' data-od-id="tab-' + n.key + '">' + ICON[n.icon] + dot +
        '<span data-nav-label="' + n.key + '">' + t('nav.' + n.key) + '</span></a>';
    }).join('') +
      '<button class="tabitem" type="button" id="tab-more" aria-expanded="false"' +
      (inOverflow ? ' aria-current="page"' : '') +
      ' aria-haspopup="dialog" data-od-id="tab-more">' + ICON.more +
      '<span id="more-badge" class="tabdot"' + (open ? '' : ' hidden') + '></span>' +
      '<span data-nav-label="more">' + t('nav.more') + '</span></button>';

    const sheetLinks = overflow.map((n) =>
      '<a class="list-row" href="' + n.href + '" data-od-id="sheet-' + n.key + '">' +
        '<span class="list-main"><span class="list-title" data-nav-label="' + n.key + '">' +
          t('nav.' + n.key) + '</span>' +
        '<span class="list-sub" data-nav-hint="' + n.key + '">' + t('nav.hint.' + n.key) + '</span></span>' +
        '<span class="list-side">' + CHEVRON + '</span>' +
      '</a>').join('');

    return '' +
      '<a class="skip-link" href="#content">' + t('common.skip') + '</a>' +
      '<header class="topnav" data-od-id="topnav"><div class="topnav-inner">' +
        '<a class="brand" href="index.html" data-od-id="brand">' +
          '<span class="brand-mark" aria-hidden="true">CT</span><span>Trading</span></a>' +
        '<span class="spacer"></span>' +
        '<button class="langswitch" type="button" id="lang-switch" data-od-id="lang-switch"></button>' +
        '<a class="nav-status" href="system.html" id="nav-status" data-od-id="nav-status">' +
          '<span class="dot"></span><span>' + t('nav.status') + '</span></a>' +
      '</div></header>' +
      '<nav class="tabbar" aria-label="Sections"><div class="tabbar-inner">' + tabs + '</div></nav>' +
      '<dialog class="sheet" id="more-sheet" aria-label="' + t('nav.more') + '" data-od-id="more-sheet"><div class="sheet-inner">' +
        '<div class="sheet-grip" aria-hidden="true"></div>' +
        '<div class="list">' + sheetLinks + '</div>' +
        '<button class="btn btn-secondary" id="more-close" data-i18n="common.close" style="margin-top:16px;">' +
          t('common.close') + '</button>' +
      '</div></dialog>';
  }


  function paintNavStatus() {
    const badge = document.getElementById('more-badge');
    if (badge) badge.hidden = !openCount();
    const el = document.getElementById('nav-status');
    if (!el) return;
    const s = systemStatus();
    const tone = s === 'READY' ? 'dot-ok' : (s === 'PAUSED' ? 'dot-warn' : 'dot-bad');
    el.innerHTML = '<span class="dot ' + tone + '"></span><span>' + escapeHtml(code(s)) + '</span>';
    el.title = sysCopy(s);
  }

  function renderFoot() {
    return '<footer class="pagefoot" data-od-id="pagefoot"><div class="container">' +
      '<p class="body-sm" data-i18n="foot.local">' + t('foot.local') + '</p>' +
      '<p class="body-sm" style="margin-top:4px;" id="foot-stamp"></p>' +
      '</div></footer>';
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

  /* ─── connection banner — fixture data must never look live ───────── */
  function paintConnection() {
    const bar = document.getElementById('conn-bar');
    if (!bar || !window.CT_API) return;
    bar.hidden = false;
    bar.className = 'conn-bar is-offline';
    const zh = window.I18N.lang === 'zh';
    if (CT_API.state.demo) {
      bar.textContent = zh ? '演示模式 · 全部为示例数据 · 所有操作关闭' : 'DEMO · Sample data only · All actions disabled';
      return;
    }
    const rows = Object.entries(CT_API.state.resources).filter(([key]) => CT_API.SOURCES[key].path());
    const missing = rows.filter(([key]) => !CT_API.isLive(key));
    if (!CT_API.authenticated) {
      bar.textContent = zh ? '未登录 · 请登录后查看账户或操作' : 'Not signed in · Sign in to view your account or act';
    } else if (!rows.length) {
      bar.textContent = zh ? '正在读取真实数据…' : 'Loading account data…';
    } else if (missing.length || !CT_API.state.online) {
      bar.textContent = (zh ? '数据不完整或已过期：' : 'Data unavailable or stale: ') +
        missing.map(([key, item]) => key + (item.error ? ' — ' + item.error : '')).join('; ');
    } else {
      bar.className = 'conn-bar';
      bar.hidden = true;
    }
    let details = document.getElementById('resource-status');
    if (!details) {
      details = document.createElement('details'); details.id = 'resource-status'; details.className = 'container body-sm';
      const summary = document.createElement('summary'); summary.textContent = zh ? '数据来源与更新时间' : 'Data sources and timestamps';
      details.appendChild(summary); bar.after(details);
    }
    details.querySelectorAll('p').forEach(node => node.remove());
    rows.forEach(([key, item]) => {
      const row = document.createElement('p');
      row.textContent = key + ' · ' + (CT_API.isLive(key) ? (zh ? '实时' : 'current') : (zh ? '过期/不可用' : 'stale/unavailable')) +
        ' · ' + (item.lastOkAt ? new Date(item.lastOkAt).toLocaleTimeString() : '—');
      details.appendChild(row);
    });
  }

  /* The switch shows the language you would move to, not the current one. */
  function paintLang() {
    const el = document.getElementById('lang-switch');
    if (!el) return;
    const keys = Object.keys(window.I18N.LANGS);
    const next = keys[(keys.indexOf(window.I18N.lang) + 1) % keys.length];
    el.textContent = window.I18N.LANGS[next];
    el.setAttribute('aria-label', 'Switch to ' + window.I18N.LANGS[next]);
  }

  function mount(activeKey) {
    document.body.insertAdjacentHTML('afterbegin', renderChrome(activeKey));
    document.body.insertAdjacentHTML('beforeend', renderFoot());
    const header = document.querySelector('.topnav');
    if (header) header.insertAdjacentHTML('afterend', '<div class="conn-bar" id="conn-bar" hidden></div>');
    window.I18N.applyStatic();
    paintLang();
    const sessionLink = document.createElement('a');
    sessionLink.href = 'login.html'; sessionLink.className = 'body-sm linkish session-link';
    const paintSession = () => {
      sessionLink.textContent = window.I18N.lang === 'zh'
        ? (CT_API.authenticated ? '退出' : '登录') : (CT_API.authenticated ? 'Sign out' : 'Sign in');
    };
    paintSession();
    document.querySelector('#more-sheet .sheet-inner').appendChild(sessionLink);
    document.addEventListener('ct:auth', paintSession);
    document.addEventListener('ct:lang', paintSession);
    sessionLink.addEventListener('click', async event => {
      if (CT_API.authenticated) {
        event.preventDefault();
        await CT_API.post('/auth/logout', {});
        window.location.assign('login.html');
      }
    });
    if (window.CT_DEMO) document.addEventListener('click', event => {
      const link = event.target.closest('a[href]');
      if (!link) return;
      const url = new URL(link.href, window.location.href);
      if (url.origin === window.location.origin && /\.html$/.test(url.pathname) && !url.pathname.endsWith('/login.html')) {
        url.searchParams.set('demo', '1'); link.href = url.href;
      }
    });
    document.getElementById('lang-switch').addEventListener('click', () => {
      const keys = Object.keys(window.I18N.LANGS);
      window.I18N.set(keys[(keys.indexOf(window.I18N.lang) + 1) % keys.length]);
    });
    /* A language change repaints the chrome and re-runs every screen render. */
    document.addEventListener('ct:lang', () => {
      paintLang();
      document.getElementById('more-sheet').setAttribute('aria-label', t('nav.more'));
      document.querySelectorAll('[data-nav-label]').forEach((el) => {
        el.textContent = t('nav.' + el.getAttribute('data-nav-label'));
      });
      document.querySelectorAll('[data-nav-hint]').forEach((el) => {
        el.textContent = t('nav.hint.' + el.getAttribute('data-nav-hint'));
      });
      try { paintNavStatus(); paintFoot(); paintConnection(); } catch (e) {}
      document.dispatchEvent(new CustomEvent('ct:data', { detail: {} }));
    });
    try { paintNavStatus(); paintFoot(); } catch (e) { /* chrome only */ }
    document.addEventListener('ct:pause-changed', paintNavStatus);

    const sheet = document.getElementById('more-sheet');
    const moreBtn = document.getElementById('tab-more');
    if (sheet && moreBtn) {
      moreBtn.addEventListener('click', () => { sheet.showModal(); moreBtn.setAttribute('aria-expanded', 'true'); });
      sheet.addEventListener('close', () => { moreBtn.setAttribute('aria-expanded', 'false'); moreBtn.focus(); });
      document.getElementById('more-close').addEventListener('click', () => sheet.close());
      sheet.addEventListener('click', (e) => { if (e.target === sheet) sheet.close(); });
    }
  }

  /* ─── live(): bind a screen to the polling layer ──────────────────
     Pass the CT keys this screen reads and a render function. render()
     waits for initial live data (or explicit demo fixtures) and runs again
     after each resource update. */
  function live(keys, render) {
    keys = [...new Set(['health', ...keys])];
    const draw = () => {
      const loaded = CT_API.loaded(keys);
      document.getElementById('content').hidden = !loaded;
      if (loaded) { try { render(); } catch (e) { console.error('[console] render failed', e); } }
      paintNavStatus(); paintFoot(); paintConnection();
    };
    draw();
    if (!window.CT_API) return;
    document.addEventListener('ct:data', draw);
    CT_API.start(keys);
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

  window.Console = {
    D, ICON, CHEVRON, NAV, LEVEL_TONE, LADDER_STEPS,
    SHOW_CONCEPTUAL, showsBucket, showsAccount,
    t, code, sysCopy, mlCopy, psCopy, bucket,
    usd, pct, dateTime, dateOnly, duration, clock, titleise,
    snapshotNow, freshnessNow, ladderMarkup, listRow, gateRow, md, escapeHtml,
    isPaused, setPaused, systemStatus, paintNavStatus, openCount,
    mount, wireTabs, live, paintConnection, paintFoot, tickValue,
  };
})();
