window.Console.page('settings', function (document) {
  /* Settings, from the phone. The page a person lands on holds the few things they decide --
     when research runs, what it studies and reads, the dates kept by hand -- as rows and
     switches. Everything else the config files hold is one tap further, under Advanced.

     Every write still goes through one pipeline on the server -- rebuild the file, validate it
     with the real loader, classify each changed value as tightening or loosening -- and one
     confirmation sheet here, which says what will change, shows the diff the server computed,
     and asks for the password when the change loosens a limit and will therefore wait a day.
     A switch is drawn from what the server last reported and moves only once the server has
     applied the change. Nothing on this page decides what is safe. */
  'use strict';
  const C = window.Console, D = C.D;
  C.mount('settings');
  const $ = (id) => document.getElementById(id);
  const h = C.escapeHtml;
  const RISK = 'risk_profile.yaml';

  const fmtAt = (iso) => (iso ? C.dateTime(iso) : '—');
  const cfg = () => D.config || null;
  const values = () => (cfg() || {}).values || {};
  const revisionOf = (file) => {
    const entry = ((cfg() || {}).files || []).find((f) => f.file === file);
    return entry ? entry.revision : '';
  };
  /* Copy for a key the dictionary may not hold: a path nobody named is shown as it is written. */
  const named = (key, fallback) => { const text = C.t(key); return text === key ? fallback : text; };
  const say = (id, text) => { $(id).textContent = text || ''; };
  /* The research card's line: what the last action here said, and otherwise a standing note
     written afresh on every draw, so it follows the language like the rest of the page. */
  let researchSaid = '';
  const researchNote = () => (cfg() && !cfg().launchd_managed ? C.t('cf.launchdUnmanaged') : '');
  const sayResearch = (text) => { researchSaid = text || ''; say('research-status', researchSaid || researchNote()); };

  /* ─── the confirmation sheet ──────────────────────────────────────────
     One sheet for a config change (a sentence, the verdict, the diff), a cash flow (a
     summary), and the autonomy stop (a reason, and a password to restore). `intent` is
     captured when the sheet opens and is what gets sent; nothing typed behind it changes it. */
  let intent = null;
  const sheet = {
    title: $('confirm-title'), summary: $('confirm-summary'), verdict: $('confirm-verdict'),
    diffBox: $('confirm-diff-box'), diff: $('confirm-diff'),
    reasonField: $('confirm-reason-field'), reason: $('confirm-reason'),
    passwordField: $('confirm-password-field'), password: $('confirm-password'),
    status: $('confirm-status'), submit: $('confirm-submit'),
  };

  function openConfirm(spec) {
    intent = spec;
    sheet.title.textContent = spec.title;
    C.setHTML(sheet.summary, spec.summary || '');
    sheet.summary.hidden = !spec.summary;
    sheet.verdict.textContent = spec.verdict || '';
    sheet.verdict.className = 'pf-fine cf-sheet-note' + (spec.cooling ? ' is-cooling' : '');
    sheet.diffBox.hidden = !spec.diff;
    sheet.diffBox.open = !!spec.diffOpen;
    C.setHTML(sheet.diff, spec.diff ? renderDiff(spec.diff) : '');
    sheet.reasonField.hidden = !spec.askReason;
    sheet.reason.value = '';
    sheet.passwordField.hidden = !spec.needsPassword;
    sheet.password.value = '';
    sheet.status.textContent = '';
    sheet.submit.textContent = spec.confirmLabel || C.t(spec.cooling ? 'cf.confirmCooling' : 'common.confirm');
    sheet.submit.className = 'btn ' + (spec.danger ? 'btn-danger' : 'btn-dark');
    sheet.submit.disabled = false;
    C.openSheet('confirm-dialog');
  }

  function renderDiff(text) {
    return text.split('\n').map((line) => {
      const cls = line.startsWith('+++') || line.startsWith('---') ? 'diff-file'
        : line.startsWith('@@') ? 'diff-hunk'
        : line.startsWith('+') ? 'diff-add'
        : line.startsWith('-') ? 'diff-del' : '';
      return '<span class="' + cls + '">' + h(line) + '</span>';
    }).join('\n');
  }

  const dismiss = () => { intent = null; C.closeSheet('confirm-dialog'); };
  $('confirm-cancel').addEventListener('click', dismiss);
  $('confirm-close').addEventListener('click', dismiss);
  $('confirm-form').addEventListener('submit', (event) => { event.preventDefault(); });
  /* The password never outlives the sheet, however the sheet was closed. */
  $('confirm-dialog').addEventListener('close', () => { sheet.password.value = ''; });

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
      if (result && result.blocked) { sheet.status.textContent = C.t('cf.writesOff'); return; }
      intent = null;
      C.closeSheet('confirm-dialog');
      if (spec.done) spec.done(result);
      CT_API.readOnce('config');
    } catch (error) {
      sheet.status.textContent = error.notSent ? C.t('cf.notSent', { err: error.message }) : error.message;
      sheet.submit.disabled = false;
    }
  });

  /* ─── a config change: preview, then confirm ───────────────────────
     `opts.summary` is the change in a sentence, already escaped; the diff is folded beneath it
     unless the diff is all there is to show (a section retyped by hand). A reason is asked for
     where the trail wants one: a change that waits, and a section retyped by hand. */
  async function reviewChange(change, opts) {
    const button = opts.button;
    if (button) button.disabled = true;
    try {
      const preview = await CT_API.post('/config/preview', change);
      if (preview.blocked) { opts.status(C.t('cf.writesOff')); return; }
      if (!preview.changed) { opts.status(C.t('cf.noChange')); return; }
      const cooling = preview.classification === 'cooling';
      opts.status('');
      openConfirm({
        title: opts.title,
        summary: opts.summary,
        cooling,
        verdict: cooling ? C.t('cf.coolingVerdict', { h: (cfg() || {}).cooling_hours || 24,
                                                     paths: (preview.reasons || []).map(pathName).join(', ') })
                         : C.t('cf.immediateVerdict'),
        diff: preview.diff, diffOpen: !opts.summary,
        askReason: cooling || !opts.summary,
        needsPassword: cooling,
        confirmLabel: cooling ? null : opts.confirmLabel,
        send: ({ reason, password }) => CT_API.post('/config/changes', { ...change, reason: reason || opts.reason || '', password }),
        done: (result) => {
          if (result.status === 'pending') opts.status(C.t('cf.parked', { at: fmtAt(result.apply_after) }));
          else opts.status(opts.applied || C.t('cf.applied', { git: (result.outcome || {}).git || '' }));
          if (opts.done) opts.done(result);
        },
      });
    } catch (error) {
      opts.status(error.message);
      /* Most often the file moved under what this page last read; read it again. */
      CT_API.readOnce('config');
    } finally {
      if (button) button.disabled = !CT_API.writesEnabled;
    }
  }

  /* ─── what a change did, in words ────────────────────────────────── */
  const pathName = (path) => named('cf.path.' + path, path);
  /* A block that holds nothing but its switch reads as the switch. */
  const flat = (v) => (v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 1 && 'enabled' in v ? v.enabled : v);
  function valueText(raw, path) {
    const v = flat(raw);
    if (v === null || v === undefined) return C.t('cf.valueUnset');
    if (v === '' || (Array.isArray(v) && !v.length)) return C.t('cf.valueEmpty');
    if (typeof v === 'boolean') return C.t(v ? 'cf.valueOn' : 'cf.valueOff');
    if (typeof v === 'number' && /_usd$/.test(path)) return C.usd(v, Number.isInteger(v) ? 0 : 2);
    if (typeof v === 'number' && path === WEEKDAY) return named('cf.day.' + v, String(v));
    const text = Array.isArray(v) && v.every((x) => x === null || typeof x !== 'object') ? v.join(', ')
      : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return text.length > 28 ? text.slice(0, 27) + '…' : text;
  }
  function describe(change) {
    const rows = change.changes || [], held = change.reasons || [];
    const lead = rows.find((row) => held.includes(row.path)) || rows[0];
    if (!lead) {
      const paths = change.paths || [];
      return paths.slice(0, 2).map(pathName).join(', ') + (paths.length > 2 ? ' +' + (paths.length - 2) : '')
        || change.key || change.file;
    }
    return pathName(lead.path) + ' ' + valueText(lead.from, lead.path) + ' → ' + valueText(lead.to, lead.path) +
      (rows.length > 1 ? ' ' + C.t('cf.andMore', { n: rows.length - 1 }) : '');
  }

  /* ─── waiting to apply ───────────────────────────────────────────── */
  function paintPending(list) {
    $('pending-section').hidden = !list.length;
    $('pending-count').textContent = String(list.length);
    C.setHTML($('pending-list'), list.map((change) =>
      '<div class="pf-row" id="pending-' + h(change.id) + '" data-od-id="pending-' + h(change.id) + '"><span class="pf-row-main">' +
      '<span class="pf-row-title">' + h(describe(change)) + '</span>' +
      '<span class="pf-row-sub">' + h(C.t('cf.appliesAt', { at: fmtAt(change.apply_after), who: change.requested_by })) + '</span></span>' +
      '<button class="pf-quiet" type="button" data-cancel="' + h(change.id) + '">' + h(C.t('cf.withdraw')) + '</button></div>').join(''));
  }
  $('pending-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-cancel]');
    if (!button || button.disabled) return;
    button.disabled = true;
    try {
      const res = await CT_API.post('/config/changes/' + encodeURIComponent(button.dataset.cancel) + '/cancel', {});
      if (res.blocked) { button.textContent = C.t('cf.writesOff'); return; }
      CT_API.readOnce('config');
    } catch (error) {
      button.disabled = false;
      button.textContent = error.message;
    }
  });

  /* ─── research: when it runs ─────────────────────────────────────── */
  const WEEKDAY = 'research_schedule.launchd.weekday', HOUR = 'research_schedule.launchd.hour',
    MINUTE = 'research_schedule.launchd.minute', BUDGET = 'research_schedule.max_analyses_per_day',
    HOLIDAY = 'research_schedule.on_non_session_day', PAUSED = 'research_schedule.run_while_paused';
  const two = (n) => String(n).padStart(2, '0');
  const known = (v) => v !== undefined && v !== null;
  /* The job fires every day once any study is daily, whatever weekday the file names. */
  const firesDaily = () => ((cfg() || {}).research || {}).fires_every === 'daily';
  function whenText(v) {
    if (!known(v[HOUR]) || !known(v[MINUTE])) return '—';
    const time = two(v[HOUR]) + ':' + two(v[MINUTE]);
    return firesDaily() || !known(v[WEEKDAY]) ? C.t('cf.everyDay', { time })
      : C.t('cf.everyWeek', { day: C.t('cf.day.' + v[WEEKDAY]), time });
  }
  const holidayCopy = (value) => C.t(value === 'skip' ? 'cf.holidaySkip' : 'cf.holidayPrevious');
  const pausedCopy = (value) => C.t(value ? 'cf.keepResearching' : 'cf.stopToo');

  /* The sheet edits a draft taken when it opens, against the revision the file had then: a
     poll does not move a choice out from under a thumb, and a file that changed meanwhile is a
     conflict the server names rather than an overwrite. */
  const sched = { weekday: 5, budget: 6, holiday: 'previous_session', paused: true, revision: '' };
  const option = (value, label, chosen) => '<button type="button" role="radio" data-value="' + h(value) +
    '" aria-checked="' + (chosen ? 'true' : 'false') + '">' + h(label) + '</button>';
  function paintScheduleForm() {
    C.setHTML($('sc-days'), [1, 2, 3, 4, 5, 6, 7].map((day) =>
      '<button type="button" class="cf-day" role="radio" data-day="' + day + '" aria-checked="' +
      (day === sched.weekday ? 'true' : 'false') + '">' + h(C.t('cf.day.' + day)) + '</button>').join(''));
    $('sc-budget').textContent = String(sched.budget);
    C.setHTML($('sc-holiday'), option('previous_session', holidayCopy('previous_session'), sched.holiday !== 'skip') +
      option('skip', holidayCopy('skip'), sched.holiday === 'skip'));
    C.setHTML($('sc-paused'), option('true', pausedCopy(true), sched.paused) + option('false', pausedCopy(false), !sched.paused));
  }
  $('schedule-row').addEventListener('click', () => {
    const v = values();
    sched.weekday = Number(v[WEEKDAY]) || 5;
    sched.budget = Number(v[BUDGET]) || 6;
    sched.holiday = v[HOLIDAY] === 'skip' ? 'skip' : 'previous_session';
    sched.paused = v[PAUSED] !== false;
    sched.revision = revisionOf(RISK);
    $('sc-time').value = known(v[HOUR]) && known(v[MINUTE]) ? two(v[HOUR]) + ':' + two(v[MINUTE]) : '';
    $('sc-days-field').hidden = firesDaily();
    $('sc-daily-note').hidden = !firesDaily();
    $('sc-more').open = false;
    say('sc-status', '');
    paintScheduleForm();
    C.openSheet('schedule-sheet');
  });
  $('sc-days').addEventListener('click', (event) => {
    const hit = event.target.closest('[data-day]');
    if (!hit) return;
    sched.weekday = Number(hit.dataset.day);
    paintScheduleForm();
  });
  $('sc-holiday').addEventListener('click', (event) => {
    const hit = event.target.closest('[data-value]');
    if (!hit) return;
    sched.holiday = hit.dataset.value === 'skip' ? 'skip' : 'previous_session';
    paintScheduleForm();
  });
  $('sc-paused').addEventListener('click', (event) => {
    const hit = event.target.closest('[data-value]');
    if (!hit) return;
    sched.paused = hit.dataset.value === 'true';
    paintScheduleForm();
  });
  const step = (by) => () => { sched.budget = Math.max(1, Math.min(50, sched.budget + by)); paintScheduleForm(); };
  $('sc-budget-less').addEventListener('click', step(-1));
  $('sc-budget-more').addEventListener('click', step(1));

  $('sc-save').addEventListener('click', () => {
    const v = values();
    const time = /^(\d{1,2}):(\d{2})/.exec($('sc-time').value || '');
    if (!time || Number(time[1]) > 23 || Number(time[2]) > 59) { say('sc-status', C.t('cf.timeNeeded')); return; }
    const next = { [HOUR]: Number(time[1]), [MINUTE]: Number(time[2]), [BUDGET]: sched.budget,
                   [HOLIDAY]: sched.holiday, [PAUSED]: sched.paused };
    if (!firesDaily()) next[WEEKDAY] = sched.weekday;
    /* Only what moved is sent, and only a setting the file declares: a value cannot create one. */
    const set = {};
    Object.keys(next).forEach((path) => { if (known(v[path]) && next[path] !== v[path]) set[path] = next[path]; });
    if (!Object.keys(set).length) { say('sc-status', C.t('cf.noChange')); return; }
    const lines = [];
    const before = whenText(v), after = whenText({ ...v, ...set });
    if (before !== after) lines.push(before + ' → ' + after);
    if (BUDGET in set) lines.push(C.t('cf.maxDaily') + C.t('cf.colon') + v[BUDGET] + ' → ' + set[BUDGET]);
    if (HOLIDAY in set) lines.push(C.t('cf.holiday') + C.t('cf.colon') + holidayCopy(v[HOLIDAY]) + ' → ' + holidayCopy(set[HOLIDAY]));
    if (PAUSED in set) lines.push(C.t('cf.whilePaused') + C.t('cf.colon') + pausedCopy(v[PAUSED]) + ' → ' + pausedCopy(set[PAUSED]));
    reviewChange({ file: RISK, revision: sched.revision, set }, {
      title: C.t('cf.scheduleConfirm'), summary: lines.map(h).join('<br>'),
      button: $('sc-save'), status: (text) => say('sc-status', text),
      done: (result) => {
        C.closeSheet('schedule-sheet');
        /* What the operating system's scheduler said, when this copy is installed under it. */
        const launchd = (result.outcome || {}).launchd;
        sayResearch(result.status === 'pending' ? C.t('cf.parked', { at: fmtAt(result.apply_after) })
          : C.t('cf.scheduleApplied') + (launchd ? ' ' + launchd : ''));
      },
    });
  });

  /* ─── research: what it studies and reads ────────────────────────────
     Three switches, each named to the server rather than addressed by key. A switch is drawn
     only when the server reports it: a guess drawn as a switch is worse than none. */
  const SWITCHES = ['candidates', 'sec_form4', 'reddit'];
  const switchRow = (name) => (((cfg() || {}).research || {}).switches || {})[name] || null;
  const candidateList = (row) => (row.tickers || []).join(C.t('cf.listJoin')) || C.t('cf.candidatesGeneric');
  function paintSwitch(name) {
    const row = switchRow(name), el = $('switch-' + name), sub = $('switch-' + name + '-sub');
    el.hidden = !row;
    if (!row) return;
    el.setAttribute('aria-checked', row.on === true ? 'true' : 'false');
    let text = '', warn = false;
    if (name === 'candidates') {
      /* The engine's own reason where it gave one, verbatim; else that no account is scheduled. */
      if (row.available === false) { text = row.why || C.t('cf.candidatesUnavailable'); warn = true; }
      else text = (row.tickers || []).length
        ? candidateList(row) + ' · ' + C.t(row.period === 'daily' ? 'cf.onceDaily' : 'cf.onceWeekly')
        : C.t('cf.candidatesNone');
    } else if (name === 'sec_form4') {
      /* On without a contact is on in name only: every read ends "not configured". */
      warn = !!row.contact && row.contact.set === false;
      text = C.t(!warn ? 'cf.form4Sub' : row.on === true ? 'cf.form4OnNoContact' : 'cf.form4NoContact');
    } else text = C.t('cf.redditSub');
    sub.textContent = text;
    sub.className = 'pf-row-sub' + (warn ? ' is-warn' : '');
    el.setAttribute('aria-disabled', row.available === false ? 'true' : 'false');
  }
  let switching = false;
  async function tapSwitch(name) {
    const row = switchRow(name);
    if (!row || switching) return;
    if (row.available === false) { sayResearch(row.why || C.t('cf.candidatesUnavailable')); return; }
    const on = row.on !== true;
    /* Said here first, with the variable to set; the server refuses the same change. */
    if (on && name === 'sec_form4' && row.contact && row.contact.set === false) {
      sayResearch(C.t('cf.form4NeedsContact', { variable: row.contact.variable }));
      return;
    }
    const label = C.t('cf.sw.' + name + '.name');
    switching = true;
    try {
      await reviewChange({ file: RISK, revision: revisionOf(RISK), switch: name, on }, {
        title: C.t('cf.sw.' + name + (on ? '.onTitle' : '.offTitle')),
        summary: h(C.t('cf.sw.' + name + (on ? '.on' : '.off'), { list: candidateList(row) })),
        confirmLabel: C.t(on ? 'cf.turnOn' : 'cf.turnOff'),
        reason: (on ? 'switched on' : 'switched off') + ' from Settings',
        applied: C.t(on ? 'cf.switchedOn' : 'cf.switchedOff', { name: label }),
        button: $('switch-' + name), status: sayResearch,
      });
    } finally { switching = false; }
  }
  SWITCHES.forEach((name) => $('switch-' + name).addEventListener('click', () => tapSwitch(name)));

  /* ─── upkeep: the macro calendar ─────────────────────────────────── */
  const today = () => new Date(C.snapshotNow()).toISOString().slice(0, 10);
  const calendar = { events: [], revision: '' };
  /* A date in this year reads as a day of it; any other year is written out. */
  function shortDate(iso) {
    if (iso.slice(0, 4) !== today().slice(0, 4)) return iso;
    const day = new Date(iso + 'T00:00:00Z');
    return isNaN(day) ? iso : day.toLocaleDateString(window.I18N.lang === 'zh' ? 'zh-CN' : 'en-US',
      { month: 'short', day: 'numeric', timeZone: 'UTC' });
  }
  function paintCalendarRow(v) {
    const through = String(v.maintained_through || '').slice(0, 10), el = $('calendar-value');
    const past = !!through && through < today();
    el.textContent = !through ? '—' : C.t(past ? 'cf.throughPast' : 'cf.through', { date: shortDate(through) });
    el.className = 'pf-row-value' + (past ? ' is-warn' : '');
  }
  function paintCalendarRows() {
    C.setHTML($('cal-events'), calendar.events.map((e, i) =>
      '<div class="pf-row"><span class="pf-row-main"><span class="pf-row-title">' + h(e.date) + ' · ' +
      h(named('cf.kind.' + e.kind, e.kind)) + '</span><span class="pf-row-sub">' + h(e.note) +
      '</span></span><button class="pf-quiet" type="button" data-cal-remove="' + i + '">' +
      h(C.t('common.remove')) + '</button></div>').join('') ||
      '<p class="pf-fine">' + h(C.t('cf.noEvents')) + '</p>');
  }
  $('calendar-row').addEventListener('click', () => {
    const v = values();
    calendar.events = (v.events || []).map((e) => ({ date: String(e.date || '').slice(0, 10), kind: e.kind || '', note: e.note || '' }));
    calendar.revision = revisionOf('event_calendar.yaml');
    $('cal-through').value = String(v.maintained_through || '').slice(0, 10);
    $('cal-date').value = ''; $('cal-note').value = '';
    $('cal-more').open = false;
    say('cal-status', '');
    paintCalendarRows();
    C.openSheet('calendar-sheet');
  });
  $('cal-events').addEventListener('click', (event) => {
    const button = event.target.closest('[data-cal-remove]');
    if (!button) return;
    calendar.events.splice(Number(button.dataset.calRemove), 1);
    paintCalendarRows();
  });
  $('cal-add').addEventListener('click', () => {
    const date = $('cal-date').value;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) { say('cal-status', C.t('cf.dateNeeded')); return; }
    calendar.events.push({ date, kind: $('cal-kind').value, note: $('cal-note').value.trim() });
    calendar.events.sort((a, b) => a.date.localeCompare(b.date));
    $('cal-date').value = ''; $('cal-note').value = '';
    say('cal-status', '');
    paintCalendarRows();
  });
  $('cal-save').addEventListener('click', () => {
    const v = values(), through = $('cal-through').value;
    const was = (v.events || []).map((e) => ({ date: String(e.date || '').slice(0, 10), kind: e.kind || '', note: e.note || '' }));
    const before = was.length, after = calendar.events.length;
    const lines = [];
    if (through !== String(v.maintained_through || '').slice(0, 10)) {
      lines.push(C.t('cf.maintainedThrough') + C.t('cf.colon') + (String(v.maintained_through || '').slice(0, 10) || '—') + ' → ' + (through || '—'));
    }
    if (before !== after) lines.push(C.t('cf.eventsChanged', { before, after }));
    else if (JSON.stringify(was) !== JSON.stringify(calendar.events)) lines.push(C.t('cf.eventsEdited', { after }));
    if (!lines.length) { say('cal-status', C.t('cf.noChange')); return; }
    reviewChange({ file: 'event_calendar.yaml', revision: calendar.revision,
                   set: { maintained_through: through, events: calendar.events } }, {
      title: C.t('cf.calendarConfirm'), summary: lines.map(h).join('<br>'),
      button: $('cal-save'), status: (text) => say('cal-status', text),
      done: () => C.closeSheet('calendar-sheet'),
    });
  });

  /* ─── upkeep: the console password ───────────────────────────────── */
  $('password-row').addEventListener('click', () => {
    $('pw-current').value = ''; $('pw-new').value = ''; $('pw-again').value = '';
    say('pw-status', '');
    C.openSheet('password-sheet');
  });
  $('password-sheet').addEventListener('close', () => { $('pw-current').value = ''; $('pw-new').value = ''; $('pw-again').value = ''; });
  $('pw-save').addEventListener('click', async () => {
    if ($('pw-new').value !== $('pw-again').value) { say('pw-status', C.t('cf.passwordMismatch')); return; }
    $('pw-save').disabled = true;
    try {
      const result = await CT_API.post('/auth/password', { current: $('pw-current').value, new: $('pw-new').value });
      if (result.blocked) { say('pw-status', C.t('cf.writesOff')); return; }
      window.location.assign('login.html?next=settings.html');
    } catch (error) {
      say('pw-status', error.message);
    } finally {
      $('pw-save').disabled = false;
    }
  });

  /* ─── upkeep: where automatic trading is switched ────────────────────
     The two switches and the brake live on the Agentic account on Home. This row only says
     which way they stand, read from the authority object where the server sends it. */
  function autonomyState(data) {
    const authority = data.authority, legacy = data.autonomy || {};
    const withdrawn = authority && authority.withdrawn ? authority.withdrawn.tripped === true : !!legacy.withdrawn;
    const armed = authority && authority.switches ? Object.keys(authority.switches).some((k) => authority.switches[k] === true)
      : !!legacy.armed;
    return { withdrawn, armed, reason: legacy.reason || (authority && authority.withdrawn && authority.withdrawn.reason) || '', at: legacy.at };
  }
  const stateWord = (state) => C.t(state.withdrawn ? 'cf.autonomyWithdrawn' : state.armed ? 'cf.autonomyArmed' : 'cf.autonomyOff');

  /* ─── advanced: every section of every file ──────────────────────── */
  const FILE_COPY = { 'risk_profile.yaml': 'cf.file.risk', 'investor_profile.yaml': 'cf.file.investor',
                      'event_calendar.yaml': 'cf.file.calendar' };
  function paintFiles(files) {
    C.setHTML($('file-list'), files.map((entry) =>
      '<details class="cf-file"><summary class="pf-row"><span class="pf-row-main"><span class="pf-row-title">' +
      h(named(FILE_COPY[entry.file] || '', entry.file)) + '</span><span class="pf-row-sub">' +
      h(entry.file + ' · ' + C.t('cf.sections', { n: entry.sections.length })) +
      '</span></span><span class="pf-chevron" aria-hidden="true"></span></summary><div class="cf-sections">' +
      entry.sections.map((s) =>
        '<button class="cf-section" type="button" data-edit-file="' + h(entry.file) + '" data-edit-key="' + h(s.key) + '">' +
        '<span class="cf-section-key">' + h(s.key) + '</span>' +
        (s.guarded ? '<span class="chip chip-warn">' + h(C.t('cf.guarded')) + '</span>' : '') +
        '<span class="cf-section-meta">' + h(C.t('cf.lines', { n: s.lines })) + '</span></button>').join('') +
      '</div></details>').join(''));
  }
  const editor = { file: null, key: null, revision: '' };
  $('file-list').addEventListener('click', async (event) => {
    const row = event.target.closest('[data-edit-key]');
    if (!row) return;
    editor.file = row.dataset.editFile; editor.key = row.dataset.editKey;
    $('editor-title').textContent = editor.file + ' › ' + editor.key;
    $('editor-copy').textContent = C.t('cf.editorHint');
    $('editor-text').value = ''; say('editor-status', C.t('common.loading'));
    C.openSheet('editor-dialog');
    try {
      const section = await CT_API.fetchJSON('/config/section?file=' + encodeURIComponent(editor.file) + '&key=' + encodeURIComponent(editor.key));
      editor.revision = section.revision;
      $('editor-text').value = section.text;
      say('editor-status', section.guarded ? C.t('cf.guardedHint') : '');
    } catch (error) {
      say('editor-status', error.message);
    }
  });
  $('editor-preview').addEventListener('click', (event) => {
    event.preventDefault();
    reviewChange({ file: editor.file, revision: editor.revision, key: editor.key, text: $('editor-text').value }, {
      title: editor.file + ' › ' + editor.key, button: $('editor-preview'),
      status: (text) => say('editor-status', text),
      done: (result) => {
        C.closeSheet('editor-dialog');
        say('advanced-status', result.status === 'pending' ? C.t('cf.parked', { at: fmtAt(result.apply_after) })
          : C.t('cf.applied', { git: (result.outcome || {}).git || '' }));
      },
    });
  });

  /* ─── advanced: the change record ────────────────────────────────── */
  function paintRecent(list) {
    $('recent-count').textContent = list.length ? String(list.length) : '';
    $('recent-empty').hidden = list.length > 0;
    C.setHTML($('recent-list'), list.map((change) => {
      const tone = change.status === 'applied' ? 'chip-accent' : change.status === 'failed' ? 'chip-danger' : '';
      return '<div class="pf-row" id="recent-' + h(change.id) + '"><span class="pf-row-main"><span class="pf-row-title">' + h(describe(change)) +
        '</span><span class="pf-row-sub">' + h(fmtAt(change.applied_at || change.requested_at) + ' · ' + change.requested_by +
        (change.error ? ' · ' + change.error : '') + ((change.outcome || {}).git ? ' · ' + change.outcome.git : '')) +
        '</span></span><span class="chip ' + tone + '">' + h(named('cf.status.' + change.status, change.status)) + '</span></div>';
    }).join(''));
  }
  $('recent-row').addEventListener('click', () => C.openSheet('recent-sheet'));

  /* ─── advanced: a cash flow recorded by hand ─────────────────────────
     Recorded, not previewed: there is no file to diff, only a ledger entry. Home books the
     figure a ledger check found; this is for the entry nobody found yet. */
  $('cashflow-row').addEventListener('click', () => { say('cash-status', ''); C.openSheet('cashflow-sheet'); });
  $('cash-save').addEventListener('click', () => {
    const amount = Number($('cash-amount').value);
    if (!(amount > 0)) { $('cash-amount').focus(); return; }
    const body = { bucket: $('cash-bucket').value, kind: $('cash-kind').value, amount_usd: amount,
                   date: $('cash-date').value || undefined, reason: $('cash-reason').value };
    openConfirm({
      title: C.t('cf.cashflow'),
      summary: h(C.t('cf.cashflowSummary', { kind: C.t('cf.' + body.kind), amount: C.usd(amount), bucket: body.bucket })),
      send: () => CT_API.post('/portfolio/cashflow', body),
      done: (result) => {
        $('cash-amount').value = ''; $('cash-reason').value = '';
        CT_API.readOnce('portfolio');
        say('cash-status', C.t('cf.cashflowDone', { bucket: result.bucket, cash: C.usd(result.cash_usd) }));
      },
    });
  });

  /* ─── advanced: the autonomy stop ────────────────────────────────────
     One gesture that withdraws autonomous action until it is restored by hand with the
     password. Home's Agentic account holds the switches themselves and the order brake. */
  function paintAutonomy(data) {
    const state = autonomyState(data);
    $('auto-state').textContent = stateWord(state);
    $('auto-state').className = 'pf-row-value' + (state.withdrawn ? ' is-warn' : '');
    $('autonomy-title').textContent = C.t(state.withdrawn ? 'cf.resumeAutonomy' : 'cf.stopAutonomy');
    $('autonomy-title').className = 'pf-row-title' + (state.withdrawn ? '' : ' cf-danger');
    $('au-state').textContent = stateWord(state);
    $('au-state').className = 'chip ' + (state.withdrawn ? 'chip-warn' : state.armed ? 'chip-danger' : '');
    say('au-detail', state.withdrawn ? state.reason + (state.at ? ' · ' + fmtAt(state.at) : '') : C.t('cf.autonomySub'));
  }
  $('autonomy-row').addEventListener('click', () => {
    const data = cfg();
    if (!data) return;
    const restore = autonomyState(data).withdrawn;
    openConfirm(restore ? {
      title: C.t('cf.resumeAutonomy'), verdict: C.t('cf.resumeAutonomyHint'), askReason: true, needsPassword: true,
      confirmLabel: C.t('cf.resumeAutonomy'),
      send: ({ reason, password }) => CT_API.post('/autonomy/resume', { reason: reason || 'restored from the console', password }),
    } : {
      title: C.t('cf.stopAutonomy'), verdict: C.t('cf.stopAutonomyHint'), askReason: true, danger: true,
      confirmLabel: C.t('cf.stopAutonomy'),
      send: ({ reason }) => CT_API.post('/autonomy/stop', { reason: reason || 'stopped from the console' }),
    });
  });

  /* ─── two views of one page ──────────────────────────────────────────
     Advanced is the same page under ?view=advanced, so the header's arrow comes back from it
     the way it comes back from any page: through history when the visit began inside the app.
     Opened cold on Advanced there is no such entry, and the arrow goes to Settings first. */
  let shown = null, mainScroll = 0;
  const wantedView = () => (new URLSearchParams(location.search).get('view') === 'advanced' ? 'advanced' : 'main');
  function showView() {
    const next = wantedView();
    if (next === shown) return;
    if (shown === 'main') mainScroll = window.scrollY || 0;
    $('cf-main').hidden = next !== 'main';
    $('cf-advanced').hidden = next !== 'advanced';
    if (shown !== null && window.scrollTo) window.scrollTo(0, next === 'advanced' ? 0 : mainScroll);
    shown = next;
  }
  document.addEventListener('click', (event) => {
    if (shown !== 'advanced' || !event.target.closest || !event.target.closest('#back-link')) return;
    const state = window.history && window.history.state;
    if (!window.history || (state && state.ctDepth > 0)) return;
    event.preventDefault();
    window.history.replaceState(state, '', 'settings.html' + (window.CT_DEMO ? '?demo=1' : ''));
    showView();
  }, true);

  /* A link from a notice names the change it is about. One that is still waiting is on the
     page; one that has applied is in the record, which opens over whichever view is showing. */
  let focusedChange = null;
  function focusChange() {
    const change = new URLSearchParams(location.search).get('change');
    if (!change || focusedChange === change || document.hidden) return;
    const data = cfg();
    const pending = ((data && data.pending) || []).some((x) => x.id === change);
    const recent = !pending && ((data && data.recent) || []).some((x) => x.id === change);
    if (!pending && !recent) return;
    if (recent) C.openSheet('recent-sheet');
    const row = $((pending ? 'pending-' : 'recent-') + change);
    if (row && row.scrollIntoView) row.scrollIntoView({ block: 'center' });
    focusedChange = change;
  }

  /* ─── render ─────────────────────────────────────────────────────── */
  function render() {
    const data = cfg();
    showView();
    if (!data) return;
    const v = data.values || {};
    paintPending(data.pending || []);
    /* Scheduled research switched off as a whole is said where its time would be. */
    const idle = v['research_schedule.enabled'] === false;
    $('schedule-value').textContent = idle ? C.t('cf.scheduleOff') : whenText(v);
    $('schedule-value').className = 'pf-row-value' + (idle ? ' is-warn' : '');
    SWITCHES.forEach(paintSwitch);
    paintCalendarRow(v);
    paintAutonomy(data);
    paintFiles(data.files || []);
    paintRecent(data.recent || []);
    say('research-status', researchSaid || researchNote());
    focusChange();
  }

  showView();
  document.addEventListener('ct:auth', render);
  /* Coming back to the page, what an earlier visit's action said is no longer news. */
  document.addEventListener('ct:route', () => {
    focusedChange = null; sayResearch(''); say('advanced-status', '');
    showView(); focusChange();
  });
  C.live(['config'], render);
});
