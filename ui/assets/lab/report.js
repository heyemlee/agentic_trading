'use strict';
/* ─────────────────────────────────────────────────────────────────────────
 * One run's report, opened over the Lab: the record the run left, then each desk's
 * own words, as the pipeline wrote them under runs/<id>/reports.
 *
 *   GET /research/{id}/trace   the desks' reports, in order
 *   GET /research/{id}         the job: its state, its error, an assistant's answer
 *
 * Three rules carried over from the page this used to live on. A report is only ever
 * the run it was opened for: a late answer for another run is dropped, and one that names
 * a different run is not drawn. A link that names an account shows only a run bound to
 * that account. And a finished run counts as read only once its result is actually on
 * screen -- never on a read, never behind a hidden or closed sheet -- because that read
 * receipt is what clears its notice on Today.
 * ───────────────────────────────────────────────────────────────────── */
window.LabReport = function (document, C, hooks) {
  const $ = (id) => document.getElementById(id);
  const h = C.escapeHtml;
  const sheet = $('trace-sheet');
  const TERMINAL = ['COMPLETE', 'FAILED', 'TIMED_OUT', 'CLAUDE_LIMIT_REACHED', 'CANCELLED'];
  const FAILED = ['FAILED', 'TIMED_OUT', 'CLAUDE_LIMIT_REACHED', 'CANCELLED'];
  let epoch = 0, reading = null;

  const recordName = (row) => row.account_label || row.account_id || C.t('rs.unknown');
  const typeName = (row) => ['full_research', 'analysis'].includes(row.task_type)
    ? C.t('rs.type.' + row.task_type) : row.task_type || C.t('rs.unknown');
  const visible = () => !document.hidden && sheet.open && !sheet.classList.contains('is-closing');

  /* A link that names a scope or an account is a promise about whose report this is. */
  function bound(row, entry) {
    if (!entry.has('scope') && !entry.has('account')) return true;
    if (!row || (entry.has('scope') && row.scope !== entry.get('scope'))) return false;
    const account = entry.get('account');
    if (!account) return true;
    if (row.account_scope_kind === 'single') return row.account_id === account;
    return entry.get('include_all_personal') === 'true' && row.account_scope_kind === 'all_personal' &&
      (row.account_ids || []).includes(account);
  }

  function recordDetails(row) {
    if (!row) return '<p class="body-sm body-muted">' + C.t('rs.recordUnavailable') + '</p>';
    const route = row.route || {}, unknown = C.t('rs.unknown');
    const kv = (key, value) => '<div class="kv-row"><dt>' + C.t('rs.meta.' + key) + '</dt><dd style="overflow-wrap:anywhere;">' + h(value ?? unknown) + '</dd></div>';
    const stamp = (value) => value ? C.dateTime(value) : unknown;
    const limitations = row.limitations || {};
    return '<details class="app-disclosure"><summary>' + C.t('rs.recordDetails') + '</summary><dl class="kv">' +
      kv('account', recordName(row)) + kv('binding', C.t('rs.binding.' + (row.account_scope_kind || 'unknown'))) +
      kv('type', typeName(row)) + kv('purpose', row.purpose ? C.t('rs.purpose.' + row.purpose) : unknown) +
      kv('route', route.name) + kv('inferred', route.inferred == null ? unknown : C.t(route.inferred ? 'rs.inferred' : 'rs.recorded')) +
      kv('version', [route.package_version, route.skill_version, route.upstream_commit].filter(Boolean).join(' · ') || unknown) +
      kv('models', Object.entries(route.models || {}).filter(([, v]) => v).map(([k, v]) => k + ': ' + v).join(' · ') || unknown) +
      kv('snapshot', stamp(row.snapshot_as_of)) + kv('prices', row.prices_as_of) + kv('retrieved', stamp(row.sources_retrieved_at_max)) +
      kv('facts', stamp(row.facts_as_of)) + kv('authority', row.authority_version) +
      kv('sources', row.evidence?.sources) + kv('web', row.evidence?.web_fetches) +
      kv('quality', limitations.data_quality) + kv('missing', (limitations.missing_information || []).join('; ') || unknown) +
      '</dl><p class="body-sm body-muted">' + C.t('rs.factTimeNote') + '</p>' +
      '<h3 style="margin-top:16px;">' + C.t('rs.counterEvidence') + '</h3><p class="body-sm" style="white-space:pre-wrap;">' + h(row.counter_evidence || unknown) + '</p>' +
      (row.counter_evidence_truncated ? '<p class="body-sm body-muted">' + C.t('rs.counterTruncated') + '</p>' : '') +
      (row.superseded_by ? '<p class="banner">' + C.t('rs.superseded') + ' ' + h(row.superseded_by) + '</p>' : '') +
      '</details>';
  }

  async function saveRead() {
    const r = reading;
    if (!r?.shown || r.attempted || !visible() || !CT_API.writesEnabled || window.CT_DEMO) return;
    const job = r.job;
    if (!job || job.opened_at || !TERMINAL.includes(job.state)) return;
    r.attempted = true;
    try {
      await CT_API.markOpened(r.runId);
      if (reading !== r) return;
      $('trace-read-status').hidden = true;
      $('trace-read-retry').hidden = true;
    } catch (error) {
      if (reading !== r || !visible()) return;
      const status = $('trace-read-status');
      status.textContent = C.t('rs.openedFailed', { err: error.message }); status.hidden = false;
      $('trace-read-retry').hidden = false;
    }
  }

  async function open(runId) {
    const mine = ++epoch;
    const record = hooks.record(runId);
    reading = null;
    $('trace-record').innerHTML = '';
    $('trace-actions').innerHTML = '';
    $('trace-read-status').hidden = true;
    $('trace-read-retry').hidden = true;
    $('trace-head').innerHTML = '<p class="agent-state">' + h(runId) + '</p>';
    $('trace-steps').innerHTML = '<p class="body-sm body-muted" style="padding-top:12px;">' + C.t('rs.traceLoading') + '</p>';
    C.openSheet('trace-sheet');

    if (window.CT_DEMO) {
      reading = { runId, record, t: C.D.labTrace?.[runId] || {}, job: null, attempted: false, shown: false };
      render(); return;
    }
    const [trace, job] = await Promise.allSettled([
      CT_API.fetchJSON('/research/' + encodeURIComponent(runId) + '/trace'),
      CT_API.fetchJSON('/research/' + encodeURIComponent(runId)),
    ]);
    if (mine !== epoch) return;
    const validTrace = trace.status === 'fulfilled' && trace.value.run_id === runId;
    const validJob = job.status === 'fulfilled' && job.value.run_key === runId;
    if (!bound(record || (validJob ? job.value : null), new URLSearchParams(window.location.search))) {
      $('trace-steps').innerHTML = '<p class="body-sm">' + C.t('rs.reportOutside') + '</p>'; return;
    }
    reading = { runId, record, t: validTrace ? trace.value : {}, job: validJob ? job.value : null,
                attempted: false, shown: false };
    render();
  }

  function render() {
    if (!reading || !visible()) return;
    const { t, job, runId } = reading;
    const about = reading.record || job || {};
    $('trace-record').innerHTML = recordDetails(reading.record || job);
    const usage = t.llm_usage || {};
    $('trace-head').innerHTML =
      '<p class="agent-state">' + h(t.ticker || about.ticker || runId) + ' · ' +
        (t.final_action || about.final_action ? h(C.code(t.final_action || about.final_action))
          : job ? h(C.code(job.state)) : C.t('home.noResult')) + '</p>' +
      '<p class="agent-note">' + [
        about.account_label || (t.bucket ? C.bucket(t.bucket) : null),
        t.trade_date || about.trade_date ? C.dateOnly(t.trade_date || about.trade_date) : null,
        /* The strength against the threshold it had to clear: why a strategy run ended in no
           trade. Recorded values only -- a missing one is left out, never drawn as zero. */
        (t.confidence ?? about.confidence) != null ? C.t('rs.traceStrength', { c: t.confidence ?? about.confidence }) : null,
        Number.isFinite(about.applicable_floor) ? C.t('rs.applicableFloor', { n: about.applicable_floor }) : null,
        usage.calls ? C.t('rs.traceCalls', { n: usage.calls }) : null,
        t.elapsed_seconds ? C.clock(t.elapsed_seconds) : null,
        (t.analysts || []).length ? t.analysts.join(' + ') : null,
      ].filter(Boolean).map(h).join(' · ') + '</p>';
    /* Strategy research and a personal analysis both ran the desks, so either can be watched
       again in the office. A name the owner still holds or watches can be researched again
       from here. */
    const replay = ['full_research', 'analysis'].includes(about.task_type || 'full_research');
    const again = hooks.canStart(t.ticker || about.ticker);
    $('trace-actions').innerHTML = (replay ? '<button type="button" class="btn btn-secondary btn-sm" data-replay="' +
        h(runId) + '">' + C.t('lab.replayHere') + '</button>' : '') +
      (again ? '<button type="button" class="btn btn-secondary btn-sm" data-start="' + h(again) + '">' +
        C.t('lab.researchAgain') + '</button>' : '');

    let group = null;
    const steps = (t.steps || []).map((st, i) => {
      const gl = C.t('tg.' + st.group) === 'tg.' + st.group ? st.group_label : C.t('tg.' + st.group);
      const gh = gl !== group ? (group = gl, '<p class="trace-group">' + h(group) + '</p>') : '';
      return gh +
        '<button class="trace-step" type="button" aria-expanded="false" aria-controls="trace-body-' + i + '">' +
          '<span class="trace-num">' + h(st.order) + '</span>' +
          '<span class="trace-label">' + (C.t('tk.' + st.key) === 'tk.' + st.key ? h(st.label) : C.t('tk.' + st.key)) + '</span>' +
          '<span class="trace-size">' + (Math.round(st.chars / 100) / 10) + 'k</span>' + C.CHEVRON +
        '</button>' +
        '<div class="trace-body md" id="trace-body-' + i + '" hidden>' + C.md(st.markdown || '') +
          (st.truncated ? '<p class="body-sm body-muted">' + C.t('rs.truncated') + '</p>' : '') + '</div>';
    }).join('');
    const failed = job && FAILED.includes(job.state);
    const result = job?.assistant_result;
    const answer = result?.answer ? '<div class="md">' + C.md(result.answer) + '</div>' : '';
    const failure = failed ? '<p class="body-sm">' + h(job.error || C.code(job.state)) + '</p>' : '';
    const list = $('trace-steps');
    list.innerHTML = failure + answer + steps || '<p class="body-sm body-muted" style="padding-top:12px;">' +
      C.t(job && ['QUEUED', 'RUNNING'].includes(job.state) ? 'rs.jobRunning' : 'rs.resultMissing') + '</p>';
    reading.shown = !!(failure || answer || steps || t.final_action);

    list.querySelectorAll('.trace-step').forEach((btn) => {
      btn.addEventListener('click', () => {
        const body = document.getElementById(btn.getAttribute('aria-controls'));
        const expanded = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', String(!expanded));
        body.hidden = expanded;
      });
    });
    saveRead();
  }

  function close() { C.closeSheet('trace-sheet'); }
  function forget() { epoch++; reading = null; }

  $('trace-read-retry').addEventListener('click', () => {
    if (reading) reading.attempted = false;
    $('trace-read-retry').hidden = true;
    saveRead();
  });
  $('trace-close').addEventListener('click', close);
  /* backdrop click and Esc are handled once, in the shared chrome */
  document.addEventListener('visibilitychange', render);
  document.addEventListener('ct:auth', (e) => {
    if (e.detail?.clearDrafts) forget();
    else if (CT_API.writesEnabled) saveRead();
  });
  document.addEventListener('ct:leave', forget);

  return { open, close, forget, get runId() { return reading?.runId || null; } };
};
