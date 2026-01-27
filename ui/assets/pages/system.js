(function () {
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
    renderDevices();

    document.getElementById('warn-count').textContent = C.t('common.open', { n: warnings.length });
    document.getElementById('warning-list').innerHTML = warnings.length
      ? warnings.map(C.noticeCard).join('')
      : '<div class="banner"><span class="banner-mark">' + C.ICON.check +
        '</span><p>' + C.t('home.noNotices') + '</p></div>';
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
      return;
    }
    const chip = $('sc-state');
    chip.textContent = C.t(s.enabled ? 'sy.scheduleOn' : 'sy.scheduleOff');
    chip.className = 'chip' + (s.enabled ? ' chip-accent' : ' chip-warn');
    $('sc-next').textContent = s.next_run_at ? C.dateTime(s.next_run_at) : '—';
    $('sc-reason').textContent = s.reason || '';
    $('sc-period').textContent = (s.period || '—') + ' · ' + (s.calendar || '—') +
      ' · ' + (s.market_timezone || '—');
    $('sc-tickers').textContent = (s.tickers || []).join(', ') || '—';
    $('sc-budget').textContent = s.analyses_today + ' / ' + s.daily_budget;
    const last = s.last_run;
    $('sc-last').textContent = last
      ? C.dateTime(last.at) + ' · ' + (last.run_keys || []).length + ' ' + C.t('sy.runsStarted')
      : C.t('sy.never');
    $('sc-skip').textContent = s.last_skip
      ? C.dateTime(s.last_skip.at) + ' · ' + s.last_skip.reason : '—';
    $('sc-paused').textContent = C.t(s.run_while_paused ? 'sy.researchContinues' : 'sy.researchStops');
  }

  /* ── background work: the loops nobody watches until they stop ───── */
  function renderOperations() {
    const $ = (id) => document.getElementById(id);
    const ops = D.operations;
    if (!ops) {
      $('op-state').textContent = C.t('sy.unknown');
      $('op-list').innerHTML = '';
      $('op-disk').textContent = '';
      return;
    }
    const rows = ops.activities || [];
    const stale = rows.filter((r) => r.overdue);
    const chip = $('op-state');
    chip.textContent = stale.length ? C.t('sy.opsLate', { n: stale.length }) : C.t('sy.opsOk');
    chip.className = 'chip' + (stale.length ? ' chip-warn' : ' chip-accent');
    $('op-list').innerHTML = rows.map((row) => C.listRow({
      id: 'op-' + row.id,
      title: C.t('sy.op.' + row.id),
      sub: row.detail || '',
      value: row.last_success_at ? C.dateTime(row.last_success_at) : C.t('sy.never'),
      /* "overdue by 0m" would be the wrong story for a job that was never loaded. */
      note: !row.overdue ? C.t('sy.onTime')
            : row.age_minutes == null ? C.t('sy.neverRan')
            : C.t('sy.overdue', { d: C.duration(row.age_minutes) }),
    })).join('');
    const disk = ops.disk || {};
    $('op-disk').textContent = disk.free_gb == null ? ''
      : C.t('sy.disk', { f: disk.free_gb.toFixed(1), p: disk.free_pct.toFixed(0),
                         r: (ops.retention || {}).runs_days });
    renderNotifications();
    renderPush();
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

  /* ── signed-in devices ───────────────────────────────────────────
     A 30-day session is what keeps the phone from asking for a password
     every day. The cost is that a device left behind stays signed in, so
     each one is listed with when it was last used and can be signed out on
     its own — without changing the password and taking every other device
     down with it. */
  function renderDevices() {
    const $ = (id) => document.getElementById(id);
    const data = D.authSessions;
    const others = $('dev-revoke-others');
    if (!data) {
      $('dev-count').textContent = C.t('sy.unknown');
      $('device-list').innerHTML = '';
      $('dev-note').textContent = '';
      others.hidden = true;
      return;
    }
    const rows = data.sessions || [];
    $('dev-count').textContent = C.t('sy.deviceCount', { n: rows.length });
    $('device-list').innerHTML = rows.map((row) => {
      const idle = (Date.now() - Date.parse(row.last_seen_at)) / 60000;
      return '<div class="list-row" data-od-id="device-' + C.escapeHtml(row.session_id) + '">' +
        '<span class="list-main">' +
          '<span class="list-title">' + C.escapeHtml(row.label) +
            (row.current ? ' <span class="chip chip-accent">' + C.t('sy.thisDevice') + '</span>' : '') +
          '</span>' +
          '<span class="list-sub">' +
            C.escapeHtml(idle < 2 ? C.t('sy.justUsed')
                                  : C.t('sy.lastUsed', { d: C.duration(idle) })) + ' · ' +
            C.escapeHtml(C.t('sy.expiresIn', { d: Math.max(0, row.days_left).toFixed(0) })) +
          '</span>' +
        '</span>' +
        '<span class="list-side">' +
          '<button class="btn btn-secondary btn-sm" type="button" data-revoke="' +
            C.escapeHtml(row.session_id) + '">' + C.t('sy.signOut') + '</button>' +
        '</span></div>';
    }).join('');
    $('dev-note').textContent = C.t('sy.deviceNote');
    others.textContent = C.t('sy.revokeOthers');
    others.hidden = rows.filter((row) => !row.current).length === 0;

    $('device-list').querySelectorAll('[data-revoke]').forEach((button) => {
      button.addEventListener('click', () => revoke(button.getAttribute('data-revoke'), button));
    });
  }

  /* POST /auth/sessions/{id}/revoke — signing out the device you are holding
     ends this session too, so the page goes back to the login screen rather
     than polling with a cookie the server has already forgotten. */
  async function revoke(sessionId, button) {
    const mine = (((D.authSessions || {}).sessions) || [])
      .some((row) => row.session_id === sessionId && row.current);
    button.disabled = true;
    button.textContent = C.t('sy.signingOut');
    try {
      const res = await CT_API.post('/auth/sessions/' + encodeURIComponent(sessionId) + '/revoke', {});
      if (res.blocked) { button.textContent = C.t('sy.switchWritesOff'); return; }
      if (mine) { window.location.assign('login.html'); return; }
      CT_API.readOnce('authSessions');
    } catch (err) {
      button.disabled = false;
      button.textContent = C.t('sy.deviceFailed', { err: err.message });
    }
  }

  document.getElementById('dev-revoke-others').addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const res = await CT_API.post('/auth/sessions/revoke-others', {});
      if (!res.blocked) {
        document.getElementById('dev-note').textContent =
          C.t('sy.revokedOthers', { n: res.revoked });
        CT_API.readOnce('authSessions');
      }
    } catch (err) {
      document.getElementById('dev-note').textContent = C.t('sy.deviceFailed', { err: err.message });
    } finally {
      button.disabled = false;
    }
  });

  /* ── notifications on this device ────────────────────────────────
     iOS delivers web push only to a page opened from its home-screen icon,
     and only after an explicit tap grants permission. Both fail quietly, so
     every state here says what it is and what to do about it. */
  async function renderPush() {
    const $ = (id) => document.getElementById(id);
    const chip = $('push-state'), detail = $('push-detail');
    const enable = $('push-enable'), test = $('push-test'), off = $('push-disable');
    if (!chip || !window.CT_PUSH) return;

    const blocked = CT_PUSH.why();
    const subscription = blocked ? null : await CT_PUSH.current().catch(() => null);
    const devices = (D.pushDevices || {}).devices || [];

    chip.textContent = C.t(blocked ? 'sy.pushBlocked' : subscription ? 'sy.pushOn' : 'sy.pushOff');
    chip.className = 'chip' + (subscription ? ' chip-accent' : blocked ? ' chip-warn' : '');
    detail.textContent = blocked ? blocked
      : subscription ? C.t('sy.pushReady') : C.t('sy.pushIdle');

    enable.hidden = Boolean(blocked) || Boolean(subscription);
    test.hidden = !subscription;
    off.hidden = !subscription;
    enable.textContent = C.t('sy.pushEnable');
    test.textContent = C.t('sy.pushTest');
    off.textContent = C.t('sy.pushDisable');

    $('push-devices').innerHTML = devices.map((device) =>
      '<div class="list-row" data-od-id="push-device-' + C.escapeHtml(device.device_id) + '">' +
        '<span class="list-main"><span class="list-title">' + C.escapeHtml(device.label) + '</span>' +
        '<span class="list-sub">' + C.escapeHtml(C.t('sy.pushDevice', { d: C.dateOnly(device.created_at) })) + '</span></span>' +
        '<span class="list-side"><span class="list-vals"><span class="list-note">' +
          C.escapeHtml(device.last_sent_at ? C.dateTime(device.last_sent_at) : '—') + '</span></span>' +
        '<button class="btn btn-secondary btn-sm" type="button" data-push-remove="' + C.escapeHtml(device.device_id) + '">' +
          C.t('sy.pushRemove') + '</button></span></div>').join('');
  }

  function wirePush() {
    const $ = (id) => document.getElementById(id);
    if (!$('push-enable') || !window.CT_PUSH) return;
    const detail = $('push-detail');

    $('push-enable').addEventListener('click', async () => {
      const button = $('push-enable');
      button.disabled = true;
      button.textContent = C.t('sy.pushWorking');
      try {
        await CT_PUSH.enable();
        CT_API.readOnce('pushDevices');
      } catch (err) {
        detail.textContent = C.t('sy.pushFailed', { err: err.message });
      } finally {
        button.disabled = false;
        renderPush();
      }
    });

    $('push-test').addEventListener('click', async () => {
      try {
        await CT_API.post('/notifications/test', {});
        detail.textContent = C.t('sy.pushSent');
      } catch (err) {
        detail.textContent = C.t('sy.pushFailed', { err: err.message });
      }
    });

    $('push-disable').addEventListener('click', async () => {
      try {
        const result = await CT_PUSH.disable();
        if (result.deviceId) await CT_API.post('/notifications/devices/' + encodeURIComponent(result.deviceId) + '/unsubscribe', {});
        CT_API.readOnce('pushDevices');
        renderPush();
      } catch (err) {
        detail.textContent = C.t('sy.pushFailed', { err: err.message });
      }
    });

    $('push-devices').addEventListener('click', async (event) => {
      const button = event.target.closest('[data-push-remove]');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = C.t('sy.pushRemoving');
      try {
        await CT_API.post('/notifications/devices/' + encodeURIComponent(button.dataset.pushRemove) + '/unsubscribe', {});
        await CT_API.readOnce('pushDevices');
        detail.textContent = C.t('sy.pushRemoved');
        renderPush();
      } catch (err) {
        button.disabled = false;
        button.textContent = C.t('sy.pushRemove');
        detail.textContent = C.t('sy.pushFailed', { err: err.message });
      }
    });
  }
  wirePush();

  function revealLinkedSetting() {
    const target = window.location.hash && document.getElementById(window.location.hash.slice(1));
    const group = target && (target.matches('details') ? target : target.closest('details'));
    if (group) group.open = true;
  }
  revealLinkedSetting();
  window.addEventListener?.('hashchange', revealLinkedSetting);

  /* ── kill switch: POST /system/pause | /system/resume ────────────── */
  const pill = document.getElementById('switch-pill');
  const title = document.getElementById('switch-title');
  const copy = document.getElementById('switch-copy');
  const btn = document.getElementById('switch-btn');
  const dlg = document.getElementById('switch-dialog');

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

  btn.addEventListener('click', () => {
    const paused = C.isPaused();
    document.getElementById('dialog-title').textContent = C.t(paused ? 'sy.release' : 'sy.engage');
    document.getElementById('dialog-copy').textContent =
      C.t(paused ? 'sy.releaseBody' : 'sy.engageBody');
    document.getElementById('reason-field').hidden = paused;
    document.getElementById('switch-confirm').textContent =
      C.t(paused ? 'sy.releaseBtn' : 'sy.pauseBtn');
    dlg.showModal();
  });
  document.getElementById('switch-cancel').addEventListener('click', () => dlg.close());
  /* POST /system/pause | /system/resume — the only state this console writes. */
  document.getElementById('switch-confirm').addEventListener('click', async () => {
    dlg.close();
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
    } catch (err) {
      copy.textContent = C.t('sy.switchFailed', { err: err.message });
    } finally {
      btn.disabled = false;
      paint();
    }
  });

  C.live(['health', 'microLive', 'schedule', 'operations', 'notifications', 'authSessions'],
         renderHealth);
})();
