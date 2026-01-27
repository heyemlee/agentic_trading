(function () {
  /* Devices and notifications used to live inside a <details> on the System
     page, which meant "System status" and "Notifications" resolved to the same
     URL — and the deep link could not land before data arrived. They are a
     page now: their own link, their own way back. */
  const C = window.Console, D = C.D;
  C.mount('notifications');

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

    const row = $('push-actions');

    /* `why()` is synchronous. When it answers, no control will ever appear
       here — so settle the row at once rather than holding room for a button
       that is not coming. */
    const blocked = CT_PUSH.why();
    if (blocked) {
      row.dataset.pending = 'false';
      chip.textContent = C.t('sy.pushBlocked');
      chip.className = 'chip chip-warn';
      detail.textContent = blocked;
      enable.hidden = test.hidden = off.hidden = true;
      paintDevices();
      return;
    }

    /* Not blocked means a control is coming; we just do not know which one.
       Hold its height across the lookup so the answer does not shove the page
       down when it lands. Labels are already in the markup, so nothing here
       has to write them. */
    row.dataset.pending = 'true';
    const subscription = await CT_PUSH.current().catch(() => null);
    row.dataset.pending = 'false';

    chip.textContent = C.t(subscription ? 'sy.pushOn' : 'sy.pushOff');
    chip.className = 'chip' + (subscription ? ' chip-accent' : '');
    detail.textContent = subscription ? C.t('sy.pushReady') : C.t('sy.pushIdle');

    enable.hidden = Boolean(subscription);
    test.hidden = !subscription;
    off.hidden = !subscription;

    paintDevices();
  }

  function paintDevices() {
    const devices = (D.pushDevices || {}).devices || [];
    document.getElementById('push-devices').innerHTML = devices.map((device) =>
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

  C.live(['authSessions', 'pushDevices'], () => { renderDevices(); renderPush(); });
})();
