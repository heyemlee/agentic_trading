(function () {
  const C = window.Console, D = C.D;
  C.mount('activity');
  C.wireTabs();

  /* Everything below is re-run on every poll cycle. */
  function render() {

    const o = D.orders;
    document.getElementById('tracked-body').innerHTML = (o.tracked_orders || []).map(x => C.listRow({
      title: x.symbol + ' · ' + C.code(x.status),
      sub: x.proposal_id + ' · ' + (x.reconciliation_error || (x.reconciled_at ? C.dateTime(x.reconciled_at) : 'Awaiting synchronization / 等待同步')),
      value: (x.filled_quantity || 0) + ' shares filled',
      note: C.bucket(x.bucket) + ' · ' + (x.venue_status || '—'),
    })).join('') || '<p class="body-sm">No tracked orders / 暂无订单记录</p>';
    document.getElementById('fill-count').textContent = C.t('ac.recorded', { n: o.fills.length });
    document.getElementById('open-count').textContent = o.error
      ? C.t('ac.venueUnreachable') : C.t('ac.live', { n: o.open_orders.length });

    document.getElementById('fills-body').innerHTML = o.fills.length
      ? '<div class="list">' + o.fills.map((f) => C.listRow({
          title: f.symbol + ' · ' + f.side,
          sub: C.dateTime(f.at) + ' · ' + C.bucket(f.bucket),
          value: C.usd(f.notional),
          note: f.quantity + ' @ ' + C.usd(f.price),
        })).join('') + '</div>'
      : '<div class="empty" data-od-id="empty-fills">' +
          '<p class="empty-title">' + C.t('ac.noFills') + '</p></div>';

    document.getElementById('open-body').innerHTML = o.open_orders.length
      ? '<div class="list">' + o.open_orders.map((x) => C.listRow({
          title: x.symbol + ' · ' + x.side,
          sub: x.status,
          value: (x.filled_quantity || 0) + ' / ' + x.quantity,
          note: x.filled_avg_price ? C.usd(x.filled_avg_price) : C.t('ac.unfilled'),
        })).join('') + '</div>'
      : o.error
        ? '<div class="empty" data-od-id="error-open">' +
            '<p class="empty-title">' + C.t('ac.venueError') + '</p><p>' + C.escapeHtml(o.error) + '</p></div>'
        : '<div class="empty" data-od-id="empty-open">' +
            '<p class="empty-title">' + C.t('ac.noOpen') + '</p></div>';


    /* ── audit trail ─────────────────────────────────────────────────── */
    const events = [];
    D.research.forEach((r) => events.push({
      at: r.created_at, kind: 'decision',
      title: (r.final_action ? C.code(r.final_action) : C.t('ac.incomplete')) + ' · ' + r.ticker,
      sub: 'Rating ' + r.portfolio_manager_rating + ', strength ' + r.confidence + ', hard risk ' +
           (r.hard_risk_verdict || 'not reached') + ' · ' + r.run_id,
    }));
    ((D.proposalDetail || {}).history || []).forEach((h) => events.push({
      at: h.at, kind: 'proposal',
      title: C.titleise(h.event) + ' · ' + D.proposalDetail.ticker,
      sub: h.detail + ' · ' + D.proposalDetail.proposal_id,
    }));
    events.sort((a, b) => Date.parse(b.at) - Date.parse(a.at));

    const auditList = (rows) => '<div class="list list-inset">' + rows.map((e) => C.listRow({
      title: e.title, sub: e.sub, note: C.dateTime(e.at),
    })).join('') + '</div>';
    document.getElementById('panel-audit-all').innerHTML = auditList(events);
    document.getElementById('panel-audit-decisions').innerHTML =
      auditList(events.filter((e) => e.kind === 'decision'));
    document.getElementById('panel-audit-proposals').innerHTML =
      auditList(events.filter((e) => e.kind === 'proposal'));
  }

  C.live(['orders', 'research', 'proposalDetail'], render);
})();
