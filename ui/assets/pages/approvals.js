(function () {
  const C = window.Console, D = C.D;
  C.mount('approvals');
  C.wireTabs();
  let intent = null, inFlight = false, uncertain = false;
  const critical = () => ['proposals', 'proposalDetail', 'health', ...(D.proposalDetail?.is_real_money ? ['microLive'] : [])];
  const canApprove = () => CT_API.canAct(critical()) && D.proposalDetail?.status === 'WAITING_FOR_APPROVAL' &&
    !D.health.paused && (D.health.market || {}).is_open === true &&
    (!D.proposalDetail.is_real_money || D.health.micro_live_enabled === true && D.microLive.preflight_ready === true);

  document.getElementById('banner-icon').innerHTML = C.ICON.lock;

  const set = (id, v) => { document.getElementById(id).textContent = v; };
  const plainRows = (arr) => (arr || []).map((x) => C.listRow({ title: x })).join('');

  /* Re-read the detail from window.CT on every call — the polling layer
     replaces it in place, so nothing here may capture it in a closure. */
  function renderDetail() {
    const p = D.proposalDetail;
    if (intent && (!window.ApprovalIntent.matches(intent, p) || !canApprove())) {
      confirmBox.checked = false;
      submitBtn.disabled = true;
      document.getElementById('approve-dialog-warning').textContent =
        '提案或当前条件已变化，请关闭后重新核对。 / Proposal or conditions changed. Review again.';
    }
    approveBtn.disabled = inFlight || uncertain || !canApprove();
    const detail = document.querySelector('[data-od-id="proposal-detail"]');
    const empty = document.getElementById('queue-empty');
    if (!p) {
      /* No open proposal. Show that, rather than leaving the last render up. */
      detail.hidden = true;
      empty.hidden = false;
      document.getElementById('live-conditions').hidden = true;
      set('queue-count', C.t('common.open', { n: C.openCount() }));
      return;
    }
    detail.hidden = false;
    empty.hidden = true;
    document.getElementById('live-conditions').hidden = false;
    const r = p.research || {};

    /* the whole head is read from the proposal, never baked into markup */
    set('d-bucket', (p.is_real_money ? C.t('bucket.realShort') + ' · ' : '') + C.bucket(p.bucket));
    set('d-id', p.proposal_id);
    set('d-shape', C.t('ap.orderShape', { action: C.code(p.action) }));
    set('d-headline', p.ticker + ' ' + C.usd(p.notional_usd));
    /* The position cap only exists inside the HR08 rule text, not as a field.
       Rather than parse prose, say the part that is structured. */
    const cap = p.hard_risk && p.hard_risk.position_cap_pct;
    set('d-weight', p.target_weight_pct == null ? ''
      : cap != null
        ? C.t('ap.weight', { w: C.pct(p.target_weight_pct), b: C.bucket(p.bucket), cap: C.pct(cap) })
        : C.t('ap.weightNoCap', { w: C.pct(p.target_weight_pct), b: C.bucket(p.bucket) }));
    set('d-strength', C.t('ap.notProbability', { c: p.confidence }));
    set('d-ref', p.reference_price != null ? C.usd(p.reference_price) : '—');
    set('d-drift', p.freshness && p.freshness.max_price_drift_pct != null
      ? '±' + C.pct(p.freshness.max_price_drift_pct) : '—');
    set('d-quality', (p.research && p.research.data_quality) || '—');
    set('detail-created', C.dateTime(p.created_at));
    set('queue-count', C.t('common.open', { n: C.openCount() }) + ' · ' + C.psCopy(p.status));
    document.getElementById('btn-approve').textContent =
      C.t('ap.approve', { v: C.usd(p.notional_usd) });

    const why = document.getElementById('wouldnot-why');
    if (why) {
      const blocked = p.is_real_money ? ((D.microLive || {}).blocking || []) : [];
      why.textContent = [
        (D.health.market || {}).is_open ? null : C.t('home.marketClosed'),
        blocked.length ? C.t('home.blocking', { ids: blocked.join(', ') }) : null,
      ].filter(Boolean).join(' · ');
      document.getElementById('live-conditions').hidden = !why.textContent;
    }

    /* the case */
    set('t-thesis', r.investment_thesis);
    set('t-bull', r.bull_case);
    set('t-bear', r.bear_case);
    set('t-entry', r.entry_logic);
    set('t-exit', r.exit_logic);
    set('t-hold', r.expected_holding_period);
    set('t-policy', (r.policy_reasons || []).join('; '));

    /* blind spots */
    document.getElementById('t-missing').innerHTML = plainRows(r.missing_information);
    document.getElementById('t-risks').innerHTML = plainRows(r.primary_risks);
    set('t-wrong', r.what_could_make_this_wrong);
    set('t-nothing', r.why_doing_nothing_may_be_better);

    /* hard risk */
    const rules = (p.hard_risk || {}).rules || [];
    set('rule-count', C.t('ap.rulesPass', { n: rules.filter((x) => x.passed).length, m: rules.length }));
    document.getElementById('rule-list').innerHTML = rules.map((x) => C.gateRow(x)).join('');

    /* history */
    document.getElementById('history-list').innerHTML = (p.history || []).map((h) =>
      C.listRow({ title: C.titleise(h.event), sub: h.detail, note: C.dateTime(h.at) })).join('');

    tick();
  }

  /* The freshness clock ticks locally between polls so the countdown stays
     smooth; handoff §15 asks for exactly this. */
  function tick() {
    const p = D.proposalDetail;
    const meter = document.getElementById('fresh-meter');
    if (!p || !meter) return;
    const f = C.freshnessNow(p);
    set('fresh-left', C.t('common.left', { d: C.duration(f.remaining) }));
    set('fresh-note', C.code(f.state) + ' · ' + C.duration(f.age) + ' / ' +
      f.ttl.toLocaleString('en-US') + 'm');
    meter.style.width = f.pctUsed.toFixed(2) + '%';
    meter.className = 'meter-fill ' +
      (f.state === 'EXPIRED' ? 'is-bad' : f.state === 'STALE' ? 'is-warn' : 'is-accent');
  }
  setInterval(tick, 1000);

  /* ── actions ─────────────────────────────────────────────────────── */
  const slot = document.getElementById('result-slot');
  const approveBtn = document.getElementById('btn-approve');

  function panel(title, tone, message, lines, extra) {
    slot.innerHTML =
      '<div class="card ' + (tone === 'accent' ? 'card-real' : 'card-quiet') +
        '" style="margin-top:20px;" data-od-id="result-panel">' +
        '<div class="row-between row-wrap" style="margin-bottom:10px;">' +
          '<span class="chip' + (tone === 'accent' ? ' chip-accent' : '') + '">' + C.escapeHtml(title) + '</span>' +
          '<span class="body-sm body-muted mono">' +
            C.dateTime(new Date(C.snapshotNow()).toISOString()) + '</span>' +
        '</div>' +
        '<p class="body-sm" style="max-width:74ch;">' + C.escapeHtml(message) + '</p>' +
        (lines && lines.length
          ? '<div class="list list-inset" style="margin-top:8px;">' +
              lines.map((x) => C.listRow({ title: x })).join('') + '</div>'
          : '') +
        (extra || '') +
      '</div>';
  }

  const pid = () => (D.proposalDetail || {}).proposal_id;
  const blockedPanel = (verb) => panel(C.t('ap.writesOff'), 'quiet',
    C.t('ap.writesOffBody', { verb: C.t(verb) }), []);
  const errPanel = (verb, err) => panel(C.t('ap.failed'), 'quiet',
    C.t('ap.failedBody', { verb: C.t(verb), err: err.message }), []);

  /* Whatever the endpoint returns is what gets rendered. Never declare a fill
     before reading `status`; a refusal is a correct result. */
  function resultPanel(res) {
    const done = res.status === 'EXECUTED';
    const o = res.order;
    const extra = o
      ? '<hr style="margin-block:16px;" /><dl class="kv">' +
          '<div class="kv-row"><dt>' + C.t('ap.clientOrderId') + '</dt><dd>' + C.escapeHtml(o.client_order_id || '—') + '</dd></div>' +
          '<div class="kv-row"><dt>' + C.t('ap.typeStatus') + '</dt><dd>' + C.escapeHtml((o.order_type || '—') + ' · ' + (o.status || '—')) + '</dd></div>' +
          '<div class="kv-row"><dt>' + C.t('ap.filled') + '</dt><dd>' + C.escapeHtml(o.filled_quantity != null ? o.filled_quantity : '—') +
            (o.filled_avg_price != null ? ' @ ' + C.usd(o.filled_avg_price) : '') + '</dd></div>' +
          (o.notional != null ? '<div class="kv-row"><dt>' + C.t('ap.notional') + '</dt><dd>' + C.usd(o.notional) + '</dd></div>' : '') +
        '</dl>' +
        '<a class="btn btn-secondary btn-sm" href="activity.html" style="margin-top:16px;">' + C.t('ap.seeActivity') + '</a>'
      : '';
    panel(C.code(res.status) || res.outcome || C.t('ap.result'), done ? 'accent' : 'quiet',
      res.message || res.error || '', res.reasons || [], extra);
  }

  /* GET /proposals/{id}/revalidate — a read, so it needs no write flag. */
  document.getElementById('btn-revalidate').addEventListener('click', async () => {
    panel(C.t('ap.checking'), 'quiet', C.t('ap.checkingBody'), []);
    try {
      const res = await CT_API.fetchJSON('/proposals/' + encodeURIComponent(pid()) + '/revalidate');
      panel(C.t('ap.dryRun', { o: res.outcome || '—' }), 'quiet',
        C.t('ap.dryRunBody'), res.reasons || []);
    } catch (err) { errPanel('ap.verbCheck', err); }
  });

  const dlg = document.getElementById('approve-dialog');
  const confirmBox = document.getElementById('approve-confirm');
  const submitBtn = document.getElementById('approve-submit');
  approveBtn.addEventListener('click', () => {
    if (!canApprove() || inFlight || uncertain) return;
    intent = window.ApprovalIntent.capture(D.proposalDetail, crypto.randomUUID());
    confirmBox.checked = false; submitBtn.disabled = true;
    document.getElementById('approve-note').value = '';
    document.getElementById('approve-dialog-warning').textContent = '';
    document.getElementById('approve-dialog-title').textContent =
      C.code(intent.action) + ' ' + C.usd(intent.notional_usd) + ' · ' + intent.ticker;
    document.getElementById('approve-dialog-terms').textContent =
      C.bucket(intent.bucket) + ' · ' + (D.proposalDetail.is_real_money ? 'REAL MONEY / 真金' : 'SIMULATED / 模拟') +
      (intent.sell_quantity != null ? ' · ' + intent.sell_quantity + ' shares' : '') +
      ' · ' + intent.proposal_id;
    dlg.showModal();
  });
  confirmBox.addEventListener('change', () => {
    submitBtn.disabled = !confirmBox.checked || !canApprove() || !window.ApprovalIntent.matches(intent, D.proposalDetail);
  });
  document.getElementById('approve-cancel').addEventListener('click', () => { intent = null; dlg.close(); });
  submitBtn.addEventListener('click', async () => {
    if (!confirmBox.checked || !canApprove() || inFlight || !window.ApprovalIntent.matches(intent, D.proposalDetail)) return;
    const approved = intent;
    inFlight = true; submitBtn.disabled = true; approveBtn.disabled = true;
    dlg.close();
    panel(C.t('ap.revalidating'), 'quiet', C.t('ap.revalidatingBody'), []);
    const { proposal_id, ...terms } = approved;
    try {
      const res = await CT_API.post('/proposals/' + encodeURIComponent(proposal_id) + '/approve', {
        ...terms, note: document.getElementById('approve-note').value.trim() || null, confirm: true,
      });
      if (res.blocked) { blockedPanel('ap.verbApprove'); return; }
      resultPanel(res);
      approveBtn.textContent = C.code(res.status || res.outcome);
      await CT_API.readOnce('proposals');
      await CT_API.readOnce('proposalDetail');
    } catch (error) {
      uncertain = true;
      panel('需要查询结果 / Check order state', 'quiet',
        error.message + ' 请查看订单状态，勿重复批准。 / Check Activity before retrying.', [],
        '<a class="btn btn-secondary" href="activity.html">查看订单 / View orders</a>');
    } finally {
      inFlight = false; intent = null;
      approveBtn.disabled = uncertain || !canApprove();
    }
  });

  const rdlg = document.getElementById('reject-dialog');
  let rejectedId = null;
  document.getElementById('btn-reject').addEventListener('click', () => { rejectedId = pid(); rdlg.showModal(); });
  document.getElementById('reject-cancel').addEventListener('click', () => rdlg.close());

  /* POST /proposals/{id}/reject */
  document.getElementById('reject-submit').addEventListener('click', async () => {
    rdlg.close();
    try {
      const res = await CT_API.post('/proposals/' + encodeURIComponent(rejectedId) + '/reject', {
        reason: document.getElementById('reject-reason').value.trim() || null,
      });
      if (res.blocked) return blockedPanel('ap.verbDecline');
      approveBtn.disabled = true;
      panel(C.code(res.status || 'REJECTED'), 'quiet',
        res.message || '' || C.t('ap.rejected'), res.reasons || []);
      CT_API.readOnce('proposals');
    } catch (err) { errPanel('ap.verbDecline', err); }
  });

  /* POST /proposals/{id}/reanalyze — marks it and starts a fresh run. */
  document.getElementById('btn-reanalyze').addEventListener('click', async () => {
    try {
      const res = await CT_API.post('/proposals/' + encodeURIComponent(pid()) + '/reanalyze', {});
      if (res.blocked) return blockedPanel('ap.verbReanalyze');
      approveBtn.disabled = true;
      panel(C.code(res.status) || C.t('ap.reanalysisStarted'), 'quiet',
        res.message || '' || C.t('ap.reanalysisBody'),
        res.reasons || (res.run_key ? ['run ' + res.run_key] : []));
      CT_API.readOnce('proposals');
    } catch (err) { errPanel('ap.verbReanalyze', err); }
  });

  /* Live writes require a verified session; demo never calls the API. */
  C.live(['proposals', 'proposalDetail', 'health', 'microLive'], renderDetail);
})();
