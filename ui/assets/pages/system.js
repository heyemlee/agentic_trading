window.Console.page('system', function (document) {
  const C = window.Console, D = C.D;
  C.mount('system');
  C.wireTabs();

  /* ── notices — re-read on every poll cycle ───────────────────────── */
  function renderHealth() {
    const warnings = D.health.warnings || [];
    const h = D.health, ml = D.microLive || {}, acct = ml.account || {};
    const $ = (id) => document.getElementById(id);

    /* every figure below is read, not baked into the markup */
    $('mk-label').textContent = C.t('sy.market', { cal: (h.market || {}).calendar || '—' });
    $('mk-state').textContent = C.t((h.market || {}).is_open ? 'sy.open' : 'sy.closed');
    $('mk-open').textContent = (h.market || {}).next_open
      ? C.dateTime((h.market.next_open || '').replace(' ', 'T')) : '—';
    $('mk-close').textContent = (h.market || {}).next_close
      ? C.dateTime((h.market.next_close || '').replace(' ', 'T')) : '—';
    $('cl-state').textContent = C.code((h.claude || {}).state);
    $('cl-billing').textContent = (h.claude || {}).billing || '—';
    $('cl-calls').textContent = (h.claude || {}).calls_today != null ? h.claude.calls_today : '—';
    $('cl-last').textContent = (h.claude || {}).last_call_at
      ? C.dateTime(h.claude.last_call_at) : '—';
    $('ml-state').textContent = C.code(ml.status);
    $('ml-conn').textContent = ml.connected == null ? '—'
      : C.t(ml.connected ? 'sy.yes' : 'sy.no') + (ml.connected ? ' · Robinhood' : '');
    $('ml-acct').textContent = acct.account_id_masked || '—';
    $('ml-block').textContent = (ml.blocking || []).join(', ') || '—';

    renderSchedule();
    renderOperations();

    document.getElementById('warn-count').textContent = C.t('common.open', { n: warnings.length });
    C.setHTML(document.getElementById('warning-list'), warnings.length
      ? warnings.map(C.noticeCard).join('')
      : '<div class="banner"><span class="banner-mark">' + C.ICON.check +
        '</span><p>' + C.t('home.noNotices') + '</p></div>');
    paint();
  }



  /* ── the schedule, read from the server rather than assumed ──────
     A phone cannot see launchd. Everything here answers one of the two
     questions a person actually has when nothing appears to be happening:
     when will it next run, and why did the last invocation do nothing. */
  function renderSchedule() {
    const $ = (id) => document.getElementById(id);
    const s = D.schedule;
    if (!s) {
      $('sc-state').textContent = C.t('sy.unknown');
      $('sc-next').textContent = '—';
      $('sc-reason').textContent = C.t('sy.scheduleUnavailable');
      $('sc-mode').textContent = '—';
      $('sc-fires').textContent = '—';
      $('sc-reserve').textContent = '—';
      $('sc-task-section').hidden = true;
      C.setHTML($('sc-tasks'), '');
      return;
    }
    const named = (prefix, value) => {
      const key = prefix + value;
      const translated = C.t(key);
      return translated === key ? C.code(value) : translated;
    };
    const chip = $('sc-state');
    chip.textContent = C.t(s.enabled ? 'sy.scheduleOn' : 'sy.scheduleOff');
    chip.className = 'chip' + (s.enabled ? ' chip-accent' : ' chip-warn');
    $('sc-next').textContent = s.next_run_at ? C.dateTime(s.next_run_at) : '—';
    $('sc-reason').textContent = s.reason || '';
    $('sc-period').textContent = (s.period || '—') + ' · ' + (s.calendar || '—') +
      ' · ' + (s.market_timezone || '—');
    $('sc-tickers').textContent = (s.tickers || []).join(', ') || '—';
    $('sc-budget').textContent = s.analyses_today + ' / ' + s.daily_budget;
    $('sc-mode').textContent = s.per_account === true ? C.t('sy.perAccount')
      : s.per_account === false ? C.t('sy.coreReview') : C.t('sy.legacySchedule');
    $('sc-fires').textContent = named('sy.periodValue.', s.fires_every || s.period || '');
    $('sc-reserve').textContent = s.reserve_for_exits == null ? '—'
      : C.t('sy.reserveOfBudget', { n: s.reserve_for_exits, total: s.daily_budget });
    const last = s.last_run;
    $('sc-last').textContent = last
      ? C.dateTime(last.at) + ' · ' + (last.run_keys || []).length + ' ' + C.t('sy.runsStarted')
      : C.t('sy.never');
    $('sc-skip').textContent = s.last_skip
      ? C.dateTime(s.last_skip.at) + ' · ' + s.last_skip.reason : '—';
    $('sc-paused').textContent = C.t(s.run_while_paused ? 'sy.researchContinues' : 'sy.researchStops');
    const tasks = Array.isArray(s.tasks) ? s.tasks : [];
    $('sc-task-section').hidden = !tasks.length;
    C.setHTML($('sc-tasks'), tasks.map((task, index) => {
      const details = [named('sy.periodValue.', task.period),
        C.t('sy.taskLimit', { n: task.max_tickers_per_run })];
      if (task.trade_date) details.push(C.t('sy.taskTradeDate', { date: C.dateOnly(task.trade_date) }));
      if ((task.tickers || []).length) details.push(C.t('sy.taskTickers', { tickers: task.tickers.join(', ') }));
      if (task.reason) details.push(task.reason);
      const lastRun = task.last_run;
      if (lastRun?.at) details.push(C.t('sy.taskLast', {
        date: C.dateTime(lastRun.at), n: (lastRun.run_keys || []).length }));
      return C.listRow({
        id: 'schedule-task-' + index,
        title: C.bucket(task.bucket) + ' · ' + named('sy.scheduleTask.', task.kind),
        sub: details.filter(Boolean).join(' · '),
        value: C.t(task.would_run_now ? 'sy.taskWouldRun' : 'sy.taskNotDue'),
      });
    }).join(''));
  }

  /* ── background work: the loops nobody watches until they stop ───── */
  function renderOperations() {
    const $ = (id) => document.getElementById(id);
    const ops = D.operations;
    if (!ops) {
      $('op-state').textContent = C.t('sy.unknown');
      $('op-state').className = 'chip';
      C.setHTML($('op-list'), '');
      $('op-disk').textContent = '';
      return;
    }
    const rows = Array.isArray(ops.activities) ? ops.activities : [];
    const states = ['current', 'awaiting_first_run', 'overdue', 'failed'];
    const problems = rows.filter(r => ['overdue', 'failed'].includes(r.state));
    const waiting = rows.filter(r => r.state === 'awaiting_first_run');
    const unknown = rows.filter(r => !states.includes(r.state));
    const fresh = window.CT_DEMO || CT_API.isLive('operations');
    const chip = $('op-state');
    const summary = [problems.length ? C.t('sy.opsNeedsAttention', { n: problems.length }) : '',
      waiting.length ? C.t('sy.opsWaiting', { n: waiting.length }) : '',
      unknown.length ? C.t('sy.opsUnknown', { n: unknown.length }) : ''].filter(Boolean);
    chip.textContent = !fresh || !rows.length ? C.t('sy.unknown') : summary.join(' · ') || C.t('sy.opsOk');
    chip.className = 'chip' + (!fresh || !rows.length ? '' : problems.length ? ' chip-warn'
      : waiting.length || unknown.length ? '' : ' chip-accent');
    const dateRow = (key, value) => '<div class="kv-row"><dt>' + C.escapeHtml(C.t(key)) + '</dt><dd>' +
      C.escapeHtml(value ? C.dateTime(value) : C.t('sy.unknown')) + '</dd></div>';
    C.setHTML($('op-list'), (!fresh ? '<p class="body-sm">' + C.escapeHtml(C.t('sy.opsStale')) + '</p>' : '') +
      rows.map(row => {
        const state = states.includes(row.state) ? row.state : 'unknown';
        // No frontend deadline arithmetic: failed can have a future deadline,
        // and the legacy overdue boolean intentionally disagrees with state.
        const failure = ['failed', 'overdue'].includes(state) ? row.last_failure : null;
        const name = C.t('sy.op.' + row.id);
        return '<section class="operation-entry" data-od-id="op-' + C.escapeHtml(row.id) + '">' +
          '<div class="row-between row-wrap"><h3 class="body-sm">' + C.escapeHtml(name === 'sy.op.' + row.id ? row.id : name) + '</h3>' +
          '<span class="chip' + (['failed', 'overdue'].includes(state) ? ' chip-warn' : '') + '">' +
          C.escapeHtml(C.t('sy.opState.' + state)) + '</span></div><dl class="kv">' +
          dateRow('sy.opDue', row.due_by) +
          (row.expected_since && !row.last_success_at ? dateRow('sy.opSince', row.expected_since) : '') +
          (row.last_success_at ? dateRow('sy.opSuccess', row.last_success_at) : '') +
          (row.last_attempt_at ? dateRow('sy.opAttempt', row.last_attempt_at) : '') + '</dl>' +
          (row.detail ? '<p class="body-sm body-muted operation-text">' + C.escapeHtml(row.detail) + '</p>' : '') +
          (failure || state === 'failed' ? '<div class="operation-failure"><p class="eyebrow">' + C.escapeHtml(C.t('sy.opFailure')) + '</p>' +
            (failure?.at ? '<p class="body-sm">' + C.escapeHtml(C.dateTime(failure.at)) + '</p>' : '') +
            '<p class="body-sm operation-text">' + C.escapeHtml(failure?.reason || C.t('sy.opFailureUnknown')) + '</p></div>' : '') + '</section>';
      }).join(''));
    const disk = ops.disk || {};
    $('op-disk').textContent = disk.free_gb == null ? ''
      : C.t('sy.disk', { f: disk.free_gb.toFixed(1), p: disk.free_pct.toFixed(0),
                         r: (ops.retention || {}).runs_days });
    renderNotifications();
  }

  /* Whether anything would actually reach a phone. Silence has two causes —
     nothing happened, and nothing is configured — and only one of them is fine. */
  function renderNotifications() {
    const $ = (id) => document.getElementById(id);
    const n = D.notifications;
    if (!n) {
      $('nt-channel').textContent = C.t('sy.unknown');
      $('nt-watch').textContent = C.t('sy.unknown');
      $('nt-detail').textContent = '';
      return;
    }
    $('nt-channel').textContent = C.code(n.channel) +
      ' · ' + C.t(n.configured ? 'sy.notifyOn' : 'sy.notifyOff');
    $('nt-watch').textContent = C.t(n.heartbeat_configured ? 'sy.notifyOn' : 'sy.notifyOff');
    $('nt-detail').textContent = n.detail || '';
  }

  function revealLinkedSetting() {
    const target = window.location.hash && document.getElementById(window.location.hash.slice(1));
    const group = target && (target.matches('details') ? target : target.closest('details'));
    if (group) group.open = true;
  }
  revealLinkedSetting();
  window.addEventListener?.('hashchange', revealLinkedSetting);
  document.addEventListener('ct:route', revealLinkedSetting);

  /* ── kill switch: POST /system/pause | /system/resume ────────────── */
  const pill = document.getElementById('switch-pill');
  const title = document.getElementById('switch-title');
  const copy = document.getElementById('switch-copy');
  const btn = document.getElementById('switch-btn');

  function paint() {
    const paused = C.isPaused();
    pill.textContent = C.code(C.systemStatus());
    pill.className = 'chip' + (paused ? ' chip-warn' : ' chip-accent');
    title.textContent = C.t(paused ? 'sy.pausedTitle' : 'sy.readyTitle');
    copy.textContent = C.t(paused ? 'sy.pausedBody' : 'sy.readyBody');
    btn.textContent = C.t(paused ? 'sy.release' : 'sy.engage');
    btn.className = paused ? 'btn btn-green' : 'btn btn-dark';
    C.paintNavStatus();
  }

  /* A reason left in the box would ride along on the next pause, which is
     the most destructive action in the app. Clear it on every exit. */
  const clearReason = () => { document.getElementById('switch-reason').value = ''; };

  btn.addEventListener('click', () => {
    const paused = C.isPaused();
    document.getElementById('dialog-title').textContent = C.t(paused ? 'sy.release' : 'sy.engage');
    document.getElementById('dialog-copy').textContent =
      C.t(paused ? 'sy.releaseBody' : 'sy.engageBody');
    document.getElementById('reason-field').hidden = paused;
    document.getElementById('switch-confirm').textContent =
      C.t(paused ? 'sy.releaseBtn' : 'sy.pauseBtn');
    C.openSheet('switch-dialog');
  });
  document.getElementById('switch-cancel').addEventListener('click', () => {
    C.closeSheet('switch-dialog');
    clearReason();
  });
  /* POST /system/pause | /system/resume — the only state this console writes. */
  document.getElementById('switch-confirm').addEventListener('click', async () => {
    C.closeSheet('switch-dialog');
    const wasPaused = C.isPaused();
    btn.disabled = true;
    try {
      const res = wasPaused
        ? await CT_API.post('/system/resume', {})
        : await CT_API.post('/system/pause', {
            reason: document.getElementById('switch-reason').value.trim() ||
                    'paused from the interface',
          });
      if (res.blocked) {
        copy.textContent = C.t('sy.switchWritesOff');
        btn.disabled = false;
        return;
      }
      /* Trust the server's answer, not the local guess. */
      C.setPaused(res.paused !== undefined ? !!res.paused : !wasPaused);
      CT_API.readOnce('health');
      clearReason();
    } catch (err) {
      copy.textContent = C.t('sy.switchFailed', { err: err.message });
    } finally {
      btn.disabled = false;
      paint();
    }
  });

  C.live(['health', 'microLive', 'schedule', 'operations', 'notifications'],
         renderHealth);
});
