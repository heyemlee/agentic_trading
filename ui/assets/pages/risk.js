(function () {
  const C = window.Console, D = C.D;
  C.mount('risk');
  C.wireTabs();

  /* Everything below is re-run on every poll cycle. */
  function render() {

    /* ── regime ──────────────────────────────────────────────────────── */
    const reg = D.risk.regime;
    const ml = D.microLive || {};
    const acct = ml.account || {};
    document.getElementById('regime-detail').textContent = reg.detail ? reg.detail + '.' : '';
    document.getElementById('regime-level').textContent = C.code(reg.level);
    const permits = reg.level === 'NORMAL' || reg.level === 'LOW_RISK';
    const rc = document.getElementById('regime-chip');
    rc.textContent = permits ? C.t('rk.permitted') : C.code(reg.level);
    rc.className = 'chip' + (permits ? ' chip-accent' : ' chip-warn');
    document.getElementById('spy-label').textContent =
      C.t('rk.spy', { sym: reg.index_symbol || 'index' });
    document.getElementById('vix-value').textContent =
      reg.vix != null ? reg.vix.toFixed(2) : '—';
    document.getElementById('vix-bar').style.width =
      reg.vix != null ? Math.min(100, (reg.vix / 40) * 100).toFixed(1) + '%' : '0%';

    /* the capital boundary card reads the account, not a baked value */
    document.getElementById('boundary-value').textContent =
      ml.capital_boundary_usd != null ? C.usd(ml.capital_boundary_usd) : '—';
    document.getElementById('acct-masked').textContent = acct.account_id_masked || '—';
    document.getElementById('acct-cash').textContent = acct.cash != null
      ? C.usd(acct.cash) + ' · ' + C.usd(acct.equity) : '—';
    document.getElementById('acct-bp').textContent =
      acct.buying_power != null ? C.usd(acct.buying_power) : '—';
    document.getElementById('acct-mode').textContent = ml.execution_mode || '—';
    const delta = ((reg.index_close - reg.index_sma200) / reg.index_sma200) * 100;
    document.getElementById('spy-delta').textContent = C.t('rk.above', { p: delta.toFixed(2) });
    /* the bar reads the 200-day average as the baseline; full track = +25% above it */
    document.getElementById('spy-bar').style.width = Math.min(100, (delta / 25) * 100).toFixed(1) + '%';

    /* ── ladders ─────────────────────────────────────────────────────── */
    const LADDERS = [['total', 'Total', 'conceptual'], ['core', 'Core', 'conceptual'],
                     ['sandbox', 'Sandbox', 'conceptual'], ['micro_live', 'Micro-Live', 'micro_live']];
    const lv = D.risk.total || {};
    document.getElementById('ladder-summary').textContent =
      C.t('rk.ladderSummary', { l: C.code(lv.level), d: C.pct(lv.drawdown_pct || 0) });
    document.getElementById('ladder-cards').innerHTML = LADDERS.map(([key, label, kind]) => {
      const l = D.risk[key];
      const real = key === 'micro_live';
      return '<div class="card' + (real ? ' card-real' : '') + '" data-od-id="ladder-' + key + '">' +
        '<div class="row-between" style="margin-bottom:8px;">' +
          '<h3>' + (key === 'total' ? C.t('rk.total') : C.bucket(key)) + '</h3>' +
          '<span class="chip' + (real ? ' chip-accent' : '') + '">' + C.escapeHtml(C.code(l.level)) + '</span></div>' +
        '<p class="figure figure-xs">' + C.pct(l.drawdown_pct) + '</p>' +
        '<p class="body-sm body-muted" style="margin-top:2px;">' +
        C.t('rk.fromHwm', { v: C.usd(l.high_water_mark_usd, 0) }) + '</p>' +
        '<div style="margin-top:16px;">' + C.ladderMarkup(l.level, kind) + '</div>' +
        '<p class="body-sm body-muted" style="margin-top:8px;">' +
          (l.manual_reset_required
            ? C.t('rk.latched')
          : C.t('rk.noReset', { d: C.dateOnly(l.updated) })) + '</p>' +
      '</div>';
    }).join('');

    /* ── gates ───────────────────────────────────────────────────────── */
    const gateList = (rows) => rows.length
      ? rows.map((g) => C.gateRow(g)).join('')
      : '<div class="empty"><p class="empty-title">' + C.t('rk.nothingBlocking') + '</p>' +
        '<p>' + C.t('rk.nothingBlockingBody') + '</p></div>';

    const checks = D.microLive.checks.slice().sort((a, b) =>
      (a.passed === b.passed) ? a.id.localeCompare(b.id) : (a.passed ? 1 : -1));
    document.getElementById('panel-gates-all').innerHTML = gateList(checks);
    document.getElementById('panel-gates-blocking').innerHTML =
      gateList(checks.filter((g) => D.microLive.blocking.includes(g.id)));
    document.getElementById('panel-gates-green').innerHTML =
      gateList(checks.filter((g) => g.passed));

    /* ── permissions ─────────────────────────────────────────────────── */
    document.getElementById('permission-list').innerHTML =
      Object.entries(D.microLive.permissions).map(([k, v]) =>
        '<div class="kv-row"><dt>' + C.escapeHtml(k.replace(/_/g, ' ')) + '</dt><dd>' +
          C.t(v ? 'common.enabled' : 'common.disabled') + '</dd></div>').join('');
  }

  C.live(['risk', 'microLive'], render);
})();
