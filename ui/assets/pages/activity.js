window.Console.page('activity', function (document) {
  const C = window.Console, D = C.D;
  C.mount('activity');
  C.wireTabs();

  /* ── orders: this side's record, re-read on every poll cycle ────────
     The venue's own open-order list was shown here too, and reading it cost
     a Robinhood handshake every poll to show what the Robinhood app already
     shows. What the app cannot show is which proposal an order came from and
     whether it reconciled, which is the tracking list below. */
  function renderOrders() {

    const o = D.orders;
    const subject = new URLSearchParams(routeSearch).get('subject');
    const tracked = (o.tracked_orders || []).filter(x => !subject || x.proposal_id === subject);
    const fills = (o.fills || []).filter(x => !subject || x.proposal_id === subject);
    C.setHTML(document.getElementById('tracked-body'), tracked.map(x => C.listRow({
      title: x.symbol + ' · ' + C.code(x.status),
      sub: x.proposal_id + ' · ' + (x.reconciliation_error || (x.reconciled_at ? C.dateTime(x.reconciled_at) : 'Awaiting synchronization / 等待同步')),
      value: (x.filled_quantity || 0) + ' shares filled',
      note: C.bucket(x.bucket) + ' · ' + (x.venue_status || '—'),
    })).join('') || '<p class="body-sm">No tracked orders / 暂无订单记录</p>');
    document.getElementById('fill-count').textContent = C.t('ac.recorded', { n: fills.length });

    C.setHTML(document.getElementById('fills-body'), fills.length
      ? '<div class="list">' + fills.map((f) => C.listRow({
          title: f.symbol + ' · ' + f.side,
          sub: C.dateTime(f.at) + ' · ' + C.bucket(f.bucket),
          value: C.usd(f.notional),
          note: f.quantity + ' @ ' + C.usd(f.price),
        })).join('') + '</div>'
      : '<div class="empty" data-od-id="empty-fills">' +
          '<p class="empty-title">' + C.t('ac.noFills') + '</p></div>');
  }

  /* ── the audit journal ───────────────────────────────────────────
     Read from the server, one page at a time, rather than assembled here
     from the research list and whichever proposal happens to be open. That
     older approach could show one proposal's story and never the system's,
     and it lost everything as soon as the queue moved on.

     Paging is by row id, not by time: several entries can share an instant,
     and a timestamp cursor would drop or repeat them at the boundary. */
  const GROUPS = {
    all: [],
    proposals: ['proposal_created', 'proposal_approved', 'proposal_rejected',
                'proposal_invalidated', 'proposal_reanalysis', 'proposal_executed'],
    fills: ['order_filled'],
    research: ['research_completed', 'research_failed'],
    system: ['system_paused', 'system_resumed'],
  };
  let group = 'all';
  let rows = [];
  let cursor = null;
  let loading = false, requestEpoch = 0;
  let routeSearch = location.search;

  function query(reset) {
    if (reset) { rows = []; cursor = null; }
    const route = new URLSearchParams(routeSearch);
    const kinds = (route.getAll('kind').length ? route.getAll('kind') : GROUPS[group]).map((k) => 'kind=' + encodeURIComponent(k)).join('&');
    return '/activity?limit=25' + (kinds ? '&' + kinds : '') +
           (route.get('subject') ? '&subject=' + encodeURIComponent(route.get('subject')) : '') +
           (cursor || route.get('cursor') ? '&cursor=' + encodeURIComponent(cursor || route.get('cursor')) : '');
  }

  async function load(reset) {
    if (loading && !reset) return;
    const epoch = ++requestEpoch;
    loading = true;
    const more = document.getElementById('audit-more');
    more.disabled = true;
    try {
      const page = await CT_API.fetchJSON(query(reset));
      if (epoch !== requestEpoch) return;
      rows = reset ? page.events : rows.concat(page.events);
      cursor = page.next_cursor;
      paint();
    } catch (err) {
      if (epoch !== requestEpoch) return;
      document.getElementById('audit-note').textContent =
        C.t('ac.auditFailed', { err: err.message });
    } finally {
      if (epoch === requestEpoch) { loading = false; more.disabled = false; }
    }
  }

  function paint() {
    const list = document.getElementById('audit-list');
    const more = document.getElementById('audit-more');
    const subject = new URLSearchParams(routeSearch).get('subject');
    document.getElementById('audit-scope').textContent = C.t('ac.entries', { n: rows.length }) + (subject ? ' · ' + subject : '');
    list.innerHTML = rows.length ? rows.map((e) => {
      const detail = e.detail || {};
      const parts = [e.subject, detail.ticker, detail.action,
                     detail.notional_usd != null ? C.usd(detail.notional_usd) : null,
                     e.actor && e.actor !== 'system' ? e.actor : null,
                     detail.error || detail.reason || null].filter(Boolean);
      const title = C.t('ev.' + e.kind) === 'ev.' + e.kind ? e.description : C.t('ev.' + e.kind);
      return '<details class="event-card" data-od-id="event-' + C.escapeHtml(e.id) + '"><summary><span><strong>' +
        C.escapeHtml(title) + '</strong><time>' + C.escapeHtml(C.dateTime(e.at)) + '</time></span><span class="chip">' +
        C.escapeHtml(e.outcome ? C.code(e.outcome) : '') + '</span></summary><p>' + C.escapeHtml(parts.join(' · ')) + '</p></details>';
    }).join('') : '<div class="empty" data-od-id="empty-audit">' +
        '<p class="empty-title">' + C.t('ac.noEvents') + '</p></div>';
    more.hidden = !cursor;
    more.textContent = C.t('ac.loadMore');
    /* The journal is the whole record; the page is only what has been fetched. */
    document.getElementById('audit-note').textContent =
      cursor ? C.t('ac.morePages') : C.t('ac.allLoaded');
  }

  function renderFilters() {
    C.setHTML(document.getElementById('audit-filters'), Object.keys(GROUPS).map((key) =>
      '<button class="chip' + (key === group ? ' chip-accent' : '') + '" type="button" ' +
        'data-group="' + key + '" data-od-id="audit-filter-' + key + '">' +
        C.t('ac.filter.' + key) + '</button>').join(''));
    document.getElementById('audit-filters').querySelectorAll('[data-group]').forEach((chip) => {
      chip.addEventListener('click', () => {
        group = chip.getAttribute('data-group');
        const route = new URLSearchParams(routeSearch); route.delete('kind'); route.delete('cursor'); routeSearch = route.toString();
        renderFilters();
        load(true);
      });
    });
  }

  document.getElementById('audit-more').addEventListener('click', () => load(false));

  C.live(['orders'], renderOrders);
  renderFilters();
  if (window.CT_DEMO) {
    rows = (D.activity || {}).events || [];
    cursor = (D.activity || {}).next_cursor || null;
    paint();
  } else {
    load(true);
  }
  document.addEventListener('ct:lang', () => { renderFilters(); paint(); });
  document.addEventListener('ct:route', () => {
    if (routeSearch === location.search) return;
    routeSearch = location.search;
    renderOrders();
    if (!window.CT_DEMO) load(true);
  });
});
