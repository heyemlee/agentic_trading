window.Console.page('settings', function (document) {
  /* Everything in config/ that is not code, from the phone. Every write goes through one
     pipeline on the server — rebuild the file, validate it with the real loader, classify
     each changed value as tightening or loosening — and one confirmation sheet here, which
     shows the diff the server computed and asks for the password when the change loosens
     a limit and will therefore wait a day. Nothing on this page decides what is safe. */
  'use strict';
  const C = window.Console, D = C.D;
  C.mount('settings');
  const $ = (id) => document.getElementById(id);

  const fmtAt = (iso) => (iso ? C.dateTime(iso) : '—');
  const cfg = () => D.config || null;
  const revisionOf = (file) => {
    const entry = ((cfg() || {}).files || []).find((f) => f.file === file);
    return entry ? entry.revision : '';
  };

  /* ─── the confirmation sheet ──────────────────────────────────────────
     One sheet for a config change (diff + verdict), a cash flow (a summary), and the
     autonomy switches (a reason, and a password to restore). `intent` is captured when the
     sheet opens and is what gets sent; typing on the page behind it changes nothing. */
  let intent = null;
  const sheet = {
    title: $('confirm-title'), verdict: $('confirm-verdict'), diff: $('confirm-diff'),
    summary: $('confirm-summary'), reasonField: $('confirm-reason-field'), reason: $('confirm-reason'),
    passwordField: $('confirm-password-field'), password: $('confirm-password'),
    status: $('confirm-status'), submit: $('confirm-submit'),
  };

  function openConfirm(spec) {
    intent = spec;
    sheet.title.textContent = spec.title;
    sheet.verdict.textContent = spec.verdict || '';
    sheet.verdict.className = 'body-sm ' + (spec.cooling ? 'neg' : 'body-muted');
    sheet.diff.hidden = !spec.diff;
    C.setHTML(sheet.diff, spec.diff ? renderDiff(spec.diff) : '');
    C.setHTML(sheet.summary, spec.summary || '');
    sheet.reasonField.hidden = spec.reason === false;
    sheet.reason.value = '';
    sheet.passwordField.hidden = !spec.needsPassword;
    sheet.password.value = '';
    sheet.status.textContent = '';
    sheet.submit.textContent = spec.confirmLabel || C.t(spec.cooling ? 'cf.confirmCooling' : 'common.confirm');
    sheet.submit.disabled = false;
    C.openSheet('confirm-dialog');
  }

  function renderDiff(text) {
    return text.split('\n').map((line) => {
      const cls = line.startsWith('+++') || line.startsWith('---') ? 'diff-file'
        : line.startsWith('@@') ? 'diff-hunk'
        : line.startsWith('+') ? 'diff-add'
        : line.startsWith('-') ? 'diff-del' : '';
      return '<span class="' + cls + '">' + C.escapeHtml(line) + '</span>';
    }).join('\n');
  }

  $('confirm-cancel').addEventListener('click', () => { intent = null; C.closeSheet('confirm-dialog'); });
  $('confirm-form').addEventListener('submit', (event) => { event.preventDefault(); });

  sheet.submit.addEventListener('click', async () => {
    if (!intent || sheet.submit.disabled) return;
    const spec = intent;
    if (spec.needsPassword && !sheet.password.value) {
      sheet.status.textContent = C.t('cf.passwordNeeded');
      return;
    }
    sheet.submit.disabled = true;
    sheet.status.textContent = C.t('cf.sending');
    try {
      const result = await spec.send({ reason: sheet.reason.value, password: sheet.password.value || undefined });
      if (result && result.blocked) { sheet.status.textContent = C.t('sy.switchWritesOff'); return; }
      intent = null;
      C.closeSheet('confirm-dialog');
      if (spec.done) spec.done(result);
      CT_API.readOnce('config');
    } catch (error) {
      sheet.status.textContent = error.notSent ? C.t('cf.notSent', { err: error.message }) : error.message;
      sheet.submit.disabled = false;
    }
  });

  /* ─── a config change: preview, then confirm ─────────────────────── */
  const pageStatus = {};
  function say(id, text) {
    if (!pageStatus[id]) return;
    pageStatus[id].textContent = text;
  }

  async function reviewChange(change, opts) {
    const button = opts.button;
    if (button) button.disabled = true;
    try {
      const preview = await CT_API.post('/config/preview', change);
      if (preview.blocked) { opts.status(C.t('sy.switchWritesOff')); return; }
      if (!preview.changed) { opts.status(C.t('cf.noChange')); return; }
      const cooling = preview.classification === 'cooling';
      const reasons = (preview.reasons || []).map(C.escapeHtml).join(', ');
      openConfirm({
        title: opts.title,
        cooling,
        verdict: cooling ? C.t('cf.coolingVerdict', { h: (cfg() || {}).cooling_hours || 24, paths: reasons })
                         : C.t('cf.immediateVerdict'),
        diff: preview.diff,
        needsPassword: cooling,
        send: ({ reason, password }) => CT_API.post('/config/changes', { ...change, reason, password }),
        done: (result) => {
          if (result.status === 'pending') opts.status(C.t('cf.parked', { at: fmtAt(result.apply_after) }));
          else opts.status(C.t('cf.applied', { git: (result.outcome || {}).git || '' }));
          if (opts.done) opts.done(result);
        },
      });
    } catch (error) {
      opts.status(error.message);
    } finally {
      if (button) button.disabled = !CT_API.writesEnabled;
    }
  }

  /* ─── forms ─────────────────────────────────────────────────────── */
  const forms = { dirty: new Set() };
  function bindDirty(id, key) {
    const el = $(id);
    el.addEventListener('input', () => forms.dirty.add(key));
    el.addEventListener('change', () => forms.dirty.add(key));
  }

  const SCHEDULE = {
    'research_schedule.period': ['sc-period', String],
    'research_schedule.launchd.weekday': ['sc-weekday', Number],
    'research_schedule.launchd.hour': ['sc-hour', Number],
    'research_schedule.launchd.minute': ['sc-minute', Number],
    'research_schedule.max_tickers_per_run': ['sc-max-tickers', Number],
    'research_schedule.max_analyses_per_day': ['sc-max-daily', Number],
    'research_schedule.on_non_session_day': ['sc-holiday', String],
    'research_schedule.run_while_paused': ['sc-paused', (v) => v === 'true'],
  };
  Object.keys(SCHEDULE).forEach((path) => bindDirty(SCHEDULE[path][0], 'schedule'));
  bindDirty('sc-tickers', 'schedule');

  function paintSchedule(values) {
    if (forms.dirty.has('schedule')) return;
    Object.keys(SCHEDULE).forEach((path) => {
      const value = values[path];
      if (value !== undefined && value !== null) $(SCHEDULE[path][0]).value = String(value);
    });
    $('sc-tickers').value = (values['research_schedule.tickers'] || []).join(', ');
  }

  $('sc-save').addEventListener('click', () => {
    const values = (cfg() || {}).values || {};
    const set = {};
    Object.keys(SCHEDULE).forEach((path) => {
      const [id, cast] = SCHEDULE[path];
      const next = cast($(id).value);
      if (Number.isNaN(next)) return;
      if (next !== values[path]) set[path] = next;
    });
    const tickers = $('sc-tickers').value.split(/[,\s]+/).map((t) => t.trim().toUpperCase()).filter(Boolean);
    if (tickers.join(',') !== (values['research_schedule.tickers'] || []).join(',')) set['research_schedule.tickers'] = tickers;
    if (!Object.keys(set).length) { say('schedule', C.t('cf.noChange')); return; }
    reviewChange({ file: 'risk_profile.yaml', revision: revisionOf('risk_profile.yaml'), set }, {
      title: C.t('cf.schedule'), button: $('sc-save'), status: (t) => say('schedule', t),
      done: (result) => {
        forms.dirty.delete('schedule');
        const launchd = (result.outcome || {}).launchd;
        if (launchd) $('sc-launchd').textContent = launchd;
      },
    });
  });
  pageStatus.schedule = $('sc-launchd');

  /* watchlist */
  let watchlist = null;
  function paintWatchlist(values) {
    if (watchlist === null || !forms.dirty.has('watchlist')) watchlist = (values['universe.core.individual_stocks.watchlist'] || []).slice();
    C.setHTML($('wl-chips'), watchlist.length
      ? watchlist.map((t) => '<button class="chip chip-ink" type="button" data-wl-remove="' + C.escapeHtml(t) + '">' +
          C.escapeHtml(t) + ' ×</button>').join('')
      : '<span class="body-sm body-muted">' + C.t('cf.watchlistEmpty') + '</span>');
  }
  $('wl-chips').addEventListener('click', (event) => {
    const chip = event.target.closest('[data-wl-remove]');
    if (!chip) return;
    watchlist = watchlist.filter((t) => t !== chip.dataset.wlRemove);
    forms.dirty.add('watchlist');
    paintWatchlist({});
  });
  function addTicker() {
    const ticker = $('wl-add').value.trim().toUpperCase();
    if (!/^[A-Z.\-]{1,10}$/.test(ticker)) return;
    if (!watchlist.includes(ticker)) watchlist.push(ticker);
    $('wl-add').value = '';
    forms.dirty.add('watchlist');
    paintWatchlist({});
  }
  $('wl-add-btn').addEventListener('click', addTicker);
  $('wl-add').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); addTicker(); } });
  $('wl-save').addEventListener('click', () => {
    reviewChange({ file: 'investor_profile.yaml', revision: revisionOf('investor_profile.yaml'),
                   set: { 'universe.core.individual_stocks.watchlist': watchlist } }, {
      title: C.t('cf.watchlist'), button: $('wl-save'), status: (t) => say('watchlist', t),
      done: () => forms.dirty.delete('watchlist'),
    });
  });
  pageStatus.watchlist = $('wl-status');

  /* calendar */
  let calendarEvents = null;
  function paintCalendar(values) {
    if (forms.dirty.has('calendar')) return;
    calendarEvents = (values.events || []).map((e) => ({ date: String(e.date || '').slice(0, 10), kind: e.kind || '', note: e.note || '' }));
    $('cal-through').value = String(values.maintained_through || '').slice(0, 10);
    paintCalendarRows();
  }
  function paintCalendarRows() {
    C.setHTML($('cal-events'), calendarEvents.map((e, i) =>
      '<div class="list-row"><span class="list-main"><span class="list-title">' + C.escapeHtml(e.date) +
      ' <span class="chip">' + C.escapeHtml(e.kind) + '</span></span><span class="list-sub">' + C.escapeHtml(e.note) +
      '</span></span><span class="list-side"><button class="btn btn-secondary btn-sm" type="button" data-cal-remove="' + i + '">' +
      C.t('common.remove') + '</button></span></div>').join(''));
  }
  $('cal-events').addEventListener('click', (event) => {
    const button = event.target.closest('[data-cal-remove]');
    if (!button) return;
    calendarEvents.splice(Number(button.dataset.calRemove), 1);
    forms.dirty.add('calendar');
    paintCalendarRows();
  });
  $('cal-add').addEventListener('click', () => {
    const date = $('cal-date').value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return;
    calendarEvents.push({ date, kind: $('cal-kind').value, note: $('cal-note').value.trim() });
    calendarEvents.sort((a, b) => a.date.localeCompare(b.date));
    $('cal-date').value = ''; $('cal-note').value = '';
    forms.dirty.add('calendar');
    paintCalendarRows();
  });
  bindDirty('cal-through', 'calendar');
  $('cal-save').addEventListener('click', () => {
    const set = { maintained_through: $('cal-through').value, events: calendarEvents };
    reviewChange({ file: 'event_calendar.yaml', revision: revisionOf('event_calendar.yaml'), set }, {
      title: C.t('cf.calendar'), button: $('cal-save'), status: (t) => say('calendar', t),
      done: () => forms.dirty.delete('calendar'),
    });
  });
  pageStatus.calendar = $('cal-status');

  /* cash flow — recorded, not previewed: there is no file to diff, only a ledger entry */
  $('cash-save').addEventListener('click', () => {
    const amount = Number($('cash-amount').value);
    if (!(amount > 0)) { $('cash-amount').focus(); return; }
    const body = { bucket: $('cash-bucket').value, kind: $('cash-kind').value, amount_usd: amount,
                   date: $('cash-date').value || undefined, reason: $('cash-reason').value };
    openConfirm({
      title: C.t('cf.cashflow'), reason: false,
      summary: C.escapeHtml(C.t('cf.cashflowSummary', { kind: C.t('cf.' + body.kind), amount: C.usd(amount), bucket: body.bucket })),
      send: () => CT_API.post('/portfolio/cashflow', body),
      done: (result) => {
        $('cash-amount').value = ''; $('cash-reason').value = '';
        CT_API.readOnce('portfolio');
        $('cash-status').textContent = C.t('cf.cashflowDone', { bucket: result.bucket, cash: C.usd(result.cash_usd) });
      },
    });
  });

  /* autonomy */
  function paintAutonomy(state) {
    const chip = $('au-state');
    if (!state) { chip.textContent = '—'; return; }
    const off = !state.armed && !state.withdrawn;
    chip.textContent = C.t(state.withdrawn ? 'cf.autonomyWithdrawn' : off ? 'cf.autonomyOff' : 'cf.autonomyArmed');
    chip.className = 'chip ' + (state.withdrawn ? 'chip-warn' : off ? '' : 'chip-danger');
    $('au-detail').textContent = state.withdrawn ? (state.reason || '') + (state.at ? ' · ' + fmtAt(state.at) : '')
      : off ? C.t('cf.autonomyOffHint') : C.t('cf.autonomyArmedHint');
    $('au-stop').hidden = Boolean(state.withdrawn);
    $('au-resume').hidden = !state.withdrawn;
  }
  $('au-stop').addEventListener('click', () => openConfirm({
    title: C.t('cf.stopAutonomy'), verdict: C.t('cf.stopAutonomyHint'),
    confirmLabel: C.t('cf.stopAutonomy'),
    send: ({ reason }) => CT_API.post('/autonomy/stop', { reason: reason || 'stopped from the console' }),
  }));
  $('au-resume').addEventListener('click', () => openConfirm({
    title: C.t('cf.resumeAutonomy'), verdict: C.t('cf.resumeAutonomyHint'), needsPassword: true,
    confirmLabel: C.t('cf.resumeAutonomy'),
    send: ({ reason, password }) => CT_API.post('/autonomy/resume', { reason: reason || 'restored from the console', password }),
  }));

  /* password */
  $('pw-save').addEventListener('click', async () => {
    const status = $('pw-status');
    if ($('pw-new').value !== $('pw-again').value) { status.textContent = C.t('cf.passwordMismatch'); return; }
    $('pw-save').disabled = true;
    try {
      const result = await CT_API.post('/auth/password', { current: $('pw-current').value, new: $('pw-new').value });
      if (result.blocked) { status.textContent = C.t('sy.switchWritesOff'); return; }
      window.location.assign('login.html?next=settings.html');
    } catch (error) {
      status.textContent = error.message;
    } finally {
      $('pw-save').disabled = false;
    }
  });

  /* ─── pending and recent ────────────────────────────────────────── */
  function describe(change) {
    const paths = (change.reasons && change.reasons.length ? change.reasons : change.paths) || [];
    return change.file + ' · ' + (paths.slice(0, 3).join(', ') + (paths.length > 3 ? ' +' + (paths.length - 3) : '') || change.key || '');
  }
  function paintPending(list) {
    const section = $('pending-section');
    section.hidden = !list.length;
    $('pending-count').textContent = String(list.length);
    C.setHTML($('pending-list'), list.map((change) =>
      '<div class="list-row" id="pending-' + C.escapeHtml(change.id) + '" data-od-id="pending-' + C.escapeHtml(change.id) + '"><span class="list-main">' +
      '<span class="list-title">' + C.escapeHtml(describe(change)) + '</span>' +
      '<span class="list-sub">' + C.escapeHtml(C.t('cf.appliesAt', { at: fmtAt(change.apply_after), who: change.requested_by })) + '</span></span>' +
      '<span class="list-side"><button class="btn btn-secondary btn-sm" type="button" data-cancel="' + C.escapeHtml(change.id) + '">' +
      C.t('cf.withdraw') + '</button></span></div>').join(''));
  }
  $('pending-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-cancel]');
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const res = await CT_API.post('/config/changes/' + encodeURIComponent(button.dataset.cancel) + '/cancel', {});
      if (res.blocked) { button.textContent = C.t('sy.switchWritesOff'); return; }
      CT_API.readOnce('config');
    } catch (error) {
      button.disabled = false;
      button.textContent = error.message;
    }
  });
  function paintRecent(list) {
    $('recent-empty').hidden = list.length > 0;
    C.setHTML($('recent-list'), list.map((change) => {
      const tone = change.status === 'applied' ? 'chip-accent' : change.status === 'failed' ? 'chip-danger' : '';
      return '<div class="list-row" id="recent-' + C.escapeHtml(change.id) + '"><span class="list-main"><span class="list-title">' + C.escapeHtml(describe(change)) +
        ' <span class="chip ' + tone + '">' + C.escapeHtml(change.status) + '</span></span>' +
        '<span class="list-sub">' + C.escapeHtml(fmtAt(change.applied_at || change.requested_at) + ' · ' + change.requested_by +
        (change.error ? ' · ' + change.error : '') + ((change.outcome || {}).git ? ' · ' + change.outcome.git : '')) +
        '</span></span></div>';
    }).join(''));
  }

  /* ─── every section of every file ───────────────────────────────── */
  function paintFiles(files) {
    C.setHTML($('file-list'), files.map((entry) =>
      '<details class="settings-group"><summary>' + C.escapeHtml(entry.file) + '</summary><div class="list list-inset">' +
      entry.sections.map((s) =>
        '<button class="list-row" type="button" data-edit-file="' + C.escapeHtml(entry.file) + '" data-edit-key="' + C.escapeHtml(s.key) + '">' +
        '<span class="list-main"><span class="list-title">' + C.escapeHtml(s.key) +
        (s.guarded ? ' <span class="chip chip-warn">' + C.t('cf.guarded') + '</span>' : '') + '</span>' +
        '<span class="list-sub">' + C.escapeHtml(C.t('cf.lines', { n: s.lines })) + '</span></span>' + C.CHEVRON + '</button>').join('') +
      '</div></details>').join(''));
  }
  const editor = { file: null, key: null, revision: '' };
  $('file-list').addEventListener('click', async (event) => {
    const row = event.target.closest('[data-edit-key]');
    if (!row) return;
    editor.file = row.dataset.editFile; editor.key = row.dataset.editKey;
    $('editor-title').textContent = editor.file + ' › ' + editor.key;
    $('editor-copy').textContent = C.t('cf.editorHint');
    $('editor-text').value = ''; $('editor-status').textContent = C.t('common.loading');
    C.openSheet('editor-dialog');
    try {
      const section = await CT_API.fetchJSON('/config/section?file=' + encodeURIComponent(editor.file) + '&key=' + encodeURIComponent(editor.key));
      editor.revision = section.revision;
      $('editor-text').value = section.text;
      $('editor-status').textContent = section.guarded ? C.t('cf.guardedHint') : '';
    } catch (error) {
      $('editor-status').textContent = error.message;
    }
  });
  $('editor-cancel').addEventListener('click', () => C.closeSheet('editor-dialog'));
  $('editor-preview').addEventListener('click', (event) => {
    event.preventDefault();
    reviewChange({ file: editor.file, revision: editor.revision, key: editor.key, text: $('editor-text').value }, {
      title: editor.file + ' › ' + editor.key, button: $('editor-preview'),
      status: (t) => { $('editor-status').textContent = t; },
      done: () => C.closeSheet('editor-dialog'),
    });
  });

  /* ─── render ─────────────────────────────────────────────────────── */
  let focusedChange = null;
  function focusChange() {
    const change = new URLSearchParams(location.search).get('change');
    if (!change || focusedChange === change) return;
    const data = cfg();
    const prefix = (data?.pending || []).some(x => x.id === change) ? 'pending-'
      : (data?.recent || []).some(x => x.id === change) ? 'recent-' : null;
    const row = prefix && $(prefix + change);
    if (row && !document.hidden) { row.scrollIntoView?.({ block: 'center' }); focusedChange = change; }
  }
  function render() {
    const data = cfg();
    if (!data) return;
    const values = data.values || {};
    paintSchedule(values);
    paintWatchlist(values);
    paintCalendar(values);
    paintAutonomy(data.autonomy);
    paintPending(data.pending || []);
    paintRecent(data.recent || []);
    paintFiles(data.files || []);
    focusChange();
    if (!data.launchd_managed && !$('sc-launchd').textContent) $('sc-launchd').textContent = C.t('cf.launchdUnmanaged');
  }

  document.addEventListener('ct:auth', render);
  document.addEventListener('ct:route', () => { focusedChange = null; focusChange(); });
  C.live(['config'], render);
});
