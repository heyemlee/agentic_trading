(function () {
  const C = window.Console, D = C.D;
  C.mount('portfolio');

  /* Everything below is re-run on every poll cycle. */
  function render() {

    const order = ['core', 'sandbox', 'micro_live'];
    const B = D.portfolio.buckets;
    const A = D.portfolio.accounts || {};
    const combined = D.portfolio.combined || { equity_usd: 0 };
    const total = combined.equity_usd;

    const SHADE = { core: 'var(--fg)', sandbox: 'var(--muted)', micro_live: 'var(--green)' };

    /* the header is live data, not a value baked into the markup */
    C.tickValue(document.getElementById('pf-total'), C.usd(total));
    /* A combined figure spanning a paper book and a funded account is a sum of
       two different kinds of money, so it says so rather than passing itself
       off as one portfolio. Each account below carries its own high-water mark
       and its own return, which is what any of these numbers can be compared
       against. */
    document.getElementById('pf-asof').textContent =
      C.t(combined.mixes_real_and_conceptual ? 'pf.totalMixed' : 'pf.totalAs',
          { d: C.dateOnly(D.portfolio.as_of) });

    /* ── per account: equity, funding, result, drawdown ──────────────── */
    document.getElementById('account-cards').innerHTML = Object.keys(A)
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
      }).join('');

    /* ── allocation ──────────────────────────────────────────────────── */
    document.getElementById('alloc-bar').innerHTML = order.filter((k) => B[k]).map((k) =>
      '<span style="width:' + (total ? (B[k].equity_usd / total) * 100 : 0).toFixed(2) +
      '%;background:' + SHADE[k] + ';"></span>').join('');
    document.getElementById('alloc-legend').innerHTML = order.filter((k) => B[k]).map((k) =>
      C.listRow({
        title: C.bucket(k),
        sub: C.t('acct.' + B[k].account),
        value: C.usd(B[k].equity_usd),
        note: C.t('pf.ofTotal', { p: C.pct(total ? (B[k].equity_usd / total) * 100 : 0) }),
      })).join('');

    /* ── bucket cards ────────────────────────────────────────────────── */
    document.getElementById('bucket-cards').innerHTML = order.filter((k) => B[k]).map((key) => {
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
    }).join('');
  }

  C.live(['portfolio'], render);
})();
