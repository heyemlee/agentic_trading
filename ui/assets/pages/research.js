window.Console.page('research', function (document) {
  const C = window.Console, D = C.D;
  C.mount('research');
  C.wireTabs();
  const startSection = document.querySelector('[data-od-id="start-run"]');
  const chartSection = document.querySelector('[data-od-id="confidence-chart"]');
  chartSection.before(startSection);
  const $ = id => document.getElementById(id);
  const scopes = ['agentic', 'demo', 'personal', 'unspecified'];
  let filter = { scope: window.CT_DEMO ? 'demo' : 'agentic', account: '', task_type: '', include_all_personal: false };
  const accounts = new Map();
  let accountDraft = '';
  let starting = false, requestDraft = null;
  const runsInScope = () => (D.researchHistory || []).filter(row => CT_API.matchesResearch(row));
  const jobsInScope = () => (D.researchQueue || []).filter(row => CT_API.matchesResearch(row));
  const recordName = row => row.account_label || row.account_id || C.t('rs.scope.unspecified');
  const typeName = row => ['full_research', 'analysis', 'portfolio_review'].includes(row.task_type)
    ? C.t('rs.type.' + row.task_type) : row.task_type || C.t('rs.unknown');
  const duration = value => Number.isFinite(value) ? C.clock(value) : '—';
  function filterLink(next) {
    const q = new URLSearchParams({ scope: next.scope });
    if (next.account) q.set('account', next.account);
    if (next.task_type) q.set('task_type', next.task_type);
    if (next.include_all_personal) q.set('include_all_personal', 'true');
    if (window.CT_DEMO) q.set('demo', '1');
    return 'research.html?' + q;
  }
  function draftFilter() {
    $('research-apply').href = filterLink({ scope: filter.scope,
      account: filter.scope === 'personal' ? $('research-account').value : '', task_type: $('research-task').value,
      include_all_personal: filter.scope === 'personal' && $('research-all').checked });
  }
  ['research-account', 'research-task', 'research-all'].forEach(id => $(id).addEventListener('change', () => {
    accountDraft = $('research-account').value; draftFilter();
  }));
  function renderFilters() {
    C.setHTML($('research-scopes'), scopes.map(scope => '<a class="chip' + (filter.scope === scope ? ' chip-accent' : '') +
      '" href="' + C.escapeHtml(filterLink({ scope })) + '"' + (filter.scope === scope ? ' aria-current="page"' : '') + '>' +
      C.t('rs.scope.' + scope) + '</a>').join(''));
    $('research-scopes').setAttribute('aria-label', C.t('rs.recordScope'));
    $('research-scope-note').textContent = C.t('rs.scopeNote.' + filter.scope);
    $('research-account-field').hidden = filter.scope !== 'personal';
    $('research-all-label').hidden = filter.scope !== 'personal';
    [...(D.researchHistory || []), ...(D.accounts?.accounts || [])].filter(row => row.scope === 'personal' && row.account_scope_kind === 'single' && row.account_id)
      .forEach(row => accounts.set(row.account_id, recordName(row)));
    if (filter.account && !accounts.has(filter.account)) accounts.set(filter.account, filter.account);
    C.setHTML($('research-account'), '<option value="">' + C.t('rs.allAccounts') + '</option>' +
      [...accounts].map(([id, label]) => '<option value="' + C.escapeHtml(id) + '">' + C.escapeHtml(label) + '</option>').join(''));
    $('research-account').value = accountDraft;
    $('research-account-status').hidden = filter.scope !== 'personal';
    $('research-account-status').textContent = !window.CT_DEMO && (!CT_API.isLive('accounts') || D.accounts?.personal_snapshot?.stale)
      ? C.t('rs.accountsPartial') : '';
    const source = CT_API.state.resources.researchHistory;
    $('research-record-status').textContent = !window.CT_DEMO && !source?.lastOkAt
      ? source?.error || C.t('common.loading') : !window.CT_DEMO && !CT_API.isLive('researchHistory')
        ? C.t('rs.recordsStale') + (source?.error ? ' · ' + source.error : '') : C.t('rs.recordLimit');
    const allowed = filter.scope === 'agentic' || filter.scope === 'demo';
    startSection.hidden = !allowed;
    Array.from($('f-bucket').options).forEach(opt => { opt.hidden = opt.disabled = filter.scope === 'agentic' ? opt.value !== 'micro_live' : opt.value === 'micro_live'; });
    if (filter.scope === 'agentic') $('f-bucket').value = 'micro_live';
    else if (filter.scope === 'demo' && !['core', 'sandbox'].includes($('f-bucket').value)) $('f-bucket').value = 'core';
    draftFilter();
  }
  function recordDetails(row) {
    if (!row) return '<p class="body-sm body-muted">' + C.t('rs.recordUnavailable') + '</p>';
    const h = C.escapeHtml, route = row.route || {}, unknown = C.t('rs.unknown');
    const kv = (key, value) => '<div class="kv-row"><dt>' + C.t('rs.meta.' + key) + '</dt><dd style="overflow-wrap:anywhere;">' + h(value ?? unknown) + '</dd></div>';
    const stamp = value => value ? C.dateTime(value) : unknown;
    const limitations = row.limitations || {};
    return '<dl class="kv">' + kv('account', recordName(row)) + kv('binding', C.t('rs.binding.' + (row.account_scope_kind || 'unknown'))) +
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
      (row.superseded_by ? '<p class="banner">' + C.t('rs.superseded') + ' <a href="#trace" data-trace="' + h(row.superseded_by) + '">' + h(row.superseded_by) + '</a></p>' : '');
  }
  const paintScope = () => {
    document.getElementById('run-scope').textContent = C.t('rs.bucket') + ' · ' + C.bucket(document.getElementById('f-bucket').value);
  };
  document.getElementById('f-bucket').addEventListener('change', paintScope);

  /* Everything below is re-run on every poll cycle. */
  function render() {
    renderFilters();
    paintScope();

    /* Runs on a hidden bucket leave the charts and the lists. The record is
       untouched: `ct-evaluate report` still counts every one of them. */
    const runs = runsInScope();
    chartSection.hidden = filter.scope === 'personal' || filter.scope === 'unspecified';
    const jobs = jobsInScope();
    C.setHTML($('research-jobs'), jobs.map(job => C.listRow({ href: '#trace', trace: job.run_key,
      title: (job.ticker || typeName(job)) + ' · ' + recordName(job),
      sub: C.code(job.state), value: job.route?.name || C.t('rs.unknown'), note: C.dateTime(job.created_at) })).join(''));

    /* ── confidence bars — filled encoding, real values only ─────────── */
    C.setHTML(document.getElementById('conf-chart'), '<div class="stack stack-4">' +
      runs.map((r) => {
        const confidence = Number.isFinite(r.confidence) ? r.confidence : null;
        const floor = Number.isFinite(r.applicable_floor) && r.applicable_floor >= 0 && r.applicable_floor <= 100
          ? r.applicable_floor : null;
        return '<div data-od-id="conf-' + C.escapeHtml(r.run_id) + '">' +
          '<div class="row-between" style="margin-bottom:6px;">' +
            '<span class="body-sm">' + C.escapeHtml(r.ticker) + ' · ' + C.escapeHtml(C.bucket(r.bucket)) +
              ' <span class="body-muted">' + C.dateOnly(r.trade_date) + '</span></span>' +
            '<span class="body-sm num">' + C.escapeHtml(confidence ?? '—') + '</span>' +
          '</div>' +
          (confidence == null ? '' : '<div style="position:relative;">' +
            '<div class="meter-track" style="height:10px;">' +
              '<div class="meter-fill' + (floor != null && confidence < floor ? ' is-warn' : ' is-accent') +
              '" style="width:' + Math.max(0, Math.min(100, confidence)) + '%"></div></div>' +
            (floor == null ? '' : '<span style="position:absolute;top:-3px;bottom:-3px;left:' + floor +
              '%;width:2px;background:var(--fg);" aria-hidden="true"></span>') +
          '</div>') +
          '<p class="body-sm body-muted" style="margin-top:6px;">' +
            C.escapeHtml(floor == null ? C.t('rs.floorUnavailable') : C.t('rs.applicableFloor', { n: floor })) + '</p>' +
        '</div>';
      }).join('') + '</div>');

    /* ── run list ────────────────────────────────────────────────────── */
    function runList(rows) {
      if (!rows.length) {
        const known = window.CT_DEMO || CT_API.isLive('researchHistory');
        return '<div class="empty"><p class="empty-title">' + C.t(known ? 'rs.noRuns' : CT_API.state.resources.researchHistory?.error ? 'rs.recordsInvalid' : 'common.loading') + '</p>' +
          '<p>' + C.t('rs.noRunsBody') + '</p></div>';
      }
      return '<div class="list">' + rows.map((r) => C.listRow({
        id: 'run-row-' + r.run_id,
        href: '#trace',
        trace: r.run_id,
        title: (r.ticker || typeName(r)) + ' · ' + recordName(r),
        sub: (r.final_action ? C.code(r.final_action) : C.t('ac.incomplete')) + ' · ' +
             C.code(r.portfolio_manager_rating) + ' · ' +
             C.t('rs.traceCalls', { n: r.llm_calls ?? '—' }) + ' · ' + duration(r.elapsed_seconds),
        value: r.confidence == null ? '—' : String(r.confidence),
        note: (r.superseded_by ? C.t('rs.supersededShort') + ' · ' : '') + C.dateOnly(r.trade_date),
      }) + '<details class="app-disclosure"><summary>' + C.t('rs.recordDetails') + '</summary>' + recordDetails(r) + '</details>').join('') + '</div>';
    }
    C.setHTML(document.getElementById('panel-runs-all'), runList(runs));
    C.setHTML(document.getElementById('panel-runs-notrade'), runList(runs.filter((r) => r.final_action === 'NO_TRADE')));
    C.setHTML(document.getElementById('panel-runs-proposal'), runList(runs.filter((r) => r.proposal_id)));

    /* Most recent decision — read from the record, not baked into markup. */
    const last = runs.find(row => !row.superseded_by);
    C.setHTML(document.getElementById('recent-card'), !last ? '' :
      '<div class="section-head">' +
        '<span class="chip chip-ink">' +
          (last.final_action ? C.escapeHtml(C.code(last.final_action)) : C.t('ac.incomplete')) + '</span>' +
        '<span class="body-sm body-muted mono">' + C.escapeHtml(last.run_id) + '</span></div>' +
      '<h3 style="font-size:20px;">' + C.escapeHtml(last.ticker || typeName(last)) + ' · ' + C.escapeHtml(recordName(last)) + ' · ' +
        C.dateOnly(last.trade_date) + '</h3>' +
      '<dl class="kv" style="margin-top:16px;">' +
        '<div class="kv-row"><dt>' + C.t('rs.rating') + '</dt><dd>' +
          C.escapeHtml(C.code(last.portfolio_manager_rating)) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.policyAction') + '</dt><dd>' +
          C.escapeHtml((last.policy_action || '—')) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.hardRiskVerdict') + '</dt><dd>' +
          C.escapeHtml((last.hard_risk_verdict || '—')) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.strength') + '</dt><dd>' +
          (last.confidence == null ? '—' : C.escapeHtml(last.confidence) + ' / 100') + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.callsElapsed') + '</dt><dd>' +
          C.escapeHtml(last.llm_calls ?? '—') + ' · ' + duration(last.elapsed_seconds) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.proposal') + '</dt><dd>' +
          C.escapeHtml((last.proposal_id || C.t('common.none'))) + '</dd></div>' +
      '</dl>');

  }

  /* Reattach to a queued/running job after a refresh or phone reconnect. */
  document.addEventListener('ct:data', () => {
    if (CT_API.activeRun) return;
    const active = jobsInScope().find((job) =>
      job.state === 'QUEUED' || job.state === 'RUNNING');
    if (active) CT_API.watchRun(active.run_key);
  });

  /* ── POST /research → poll GET /research/{run_key} ───────────────── */
  const hint = document.getElementById('run-hint');
  const bucketSel = document.getElementById('f-bucket');
  /* One switch governs the whole console: a bucket nobody can see is not a
     bucket anyone can start a run against either. */
  const paintHint = () => {
    hint.textContent = bucketSel.value === 'micro_live'
      ? C.t('rs.hintMicro') : C.t('rs.hintConceptual');
  };
  paintHint();
  bucketSel.addEventListener('change', paintHint);

  /* ── agent trace: GET /research/{run_id}/trace ────────────────────
     The pipeline writes one report per agent under runs/<id>/reports. The
     endpoint returns them in order, so a run reads as a sequence of steps. */
  const traceSheet = document.getElementById('trace-sheet');
  let traceEpoch = 0, traceReading = null;
  const traceVisible = () => !document.hidden && traceSheet.open && !traceSheet.classList.contains('is-closing');
  async function saveRead() {
    const reading = traceReading;
    if (!reading?.shown || reading.attempted || !traceVisible() || !CT_API.writesEnabled || window.CT_DEMO) return;
    const job = reading.job;
    if (!job || job.opened_at || !['COMPLETE', 'FAILED', 'TIMED_OUT', 'CLAUDE_LIMIT_REACHED', 'CANCELLED'].includes(job.state)) return;
    reading.attempted = true;
    try {
      await CT_API.markOpened(reading.runId);
      if (traceReading !== reading) return;
      document.getElementById('trace-read-status').hidden = true;
      document.getElementById('trace-read-retry').hidden = true;
    } catch (error) {
      if (traceReading !== reading || !traceVisible()) return;
      const status = document.getElementById('trace-read-status');
      status.textContent = C.t('rs.openedFailed', { err: error.message }); status.hidden = false;
      document.getElementById('trace-read-retry').hidden = false;
    }
  }
  document.getElementById('trace-read-retry').addEventListener('click', () => {
    if (traceReading) traceReading.attempted = false;
    document.getElementById('trace-read-retry').hidden = true;
    saveRead();
  });
  document.getElementById('trace-close').addEventListener('click', () => C.closeSheet('trace-sheet'));
  /* backdrop click and Esc are handled once, in the shared chrome */
  document.addEventListener('click', (e) => {
    const row = e.target.closest && e.target.closest('[data-trace]');
    if (!row) return;
    e.preventDefault();
    openTrace(row.getAttribute('data-trace'));
  });

  async function openTrace(runId) {
    const epoch = ++traceEpoch;
    const record = runsInScope().find(row => row.run_id === runId);
    traceReading = null;
    $('trace-record').innerHTML = '';
    document.getElementById('trace-read-status').hidden = true;
    document.getElementById('trace-read-retry').hidden = true;
    const head = document.getElementById('trace-head');
    const list = document.getElementById('trace-steps');
    head.innerHTML = '<p class="agent-state">' + C.escapeHtml(runId) + '</p>';
    list.innerHTML = '<p class="body-sm body-muted" style="padding-top:12px;">' + C.t('rs.traceLoading') + '</p>';
    C.openSheet('trace-sheet');

    if (window.CT_DEMO) {
      traceReading = { runId, record, t: D.labTrace?.[runId] || {}, job: null, attempted: false, shown: false };
      renderTrace(); return;
    }

    const [trace, job] = await Promise.allSettled([
      CT_API.fetchJSON('/research/' + encodeURIComponent(runId) + '/trace'),
      CT_API.fetchJSON('/research/' + encodeURIComponent(runId)),
    ]);
    if (epoch !== traceEpoch) return;
    const validTrace = trace.status === 'fulfilled' && trace.value.run_id === runId;
    const validJob = job.status === 'fulfilled' && job.value.run_key === runId;
    const binding = record || (validJob ? job.value : null);
    const entry = new URLSearchParams(location.search);
    if ((entry.has('scope') || entry.has('account')) && !CT_API.matchesResearch(binding, false)) {
      list.innerHTML = '<p class="body-sm">' + C.t('rs.reportOutside') + '</p>'; return;
    }
    traceReading = { runId, record, t: validTrace ? trace.value : {}, job: validJob ? job.value : null,
      failed: !validTrace && !validJob, attempted: false, shown: false };
    renderTrace();
  }
  function renderTrace() {
    if (!traceReading || !traceVisible()) return;
    const { t, job, runId } = traceReading;
    $('trace-record').innerHTML = recordDetails(traceReading.record || job);
    const head = document.getElementById('trace-head'), list = document.getElementById('trace-steps');
    const usage = t.llm_usage || {};
    head.innerHTML =
      '<p class="agent-state">' + C.escapeHtml(t.ticker || runId) + ' · ' +
        (t.final_action ? C.escapeHtml(C.code(t.final_action)) : job ? C.escapeHtml(C.code(job.state)) : C.t('home.noResult')) + '</p>' +
      '<p class="agent-note">' + [
        t.bucket ? C.bucket(t.bucket) : null,
        t.trade_date ? C.dateOnly(t.trade_date) : null,
        t.confidence != null ? C.t('rs.traceStrength', { c: t.confidence }) : null,
        usage.calls ? C.t('rs.traceCalls', { n: usage.calls }) : null,
        t.elapsed_seconds ? C.clock(t.elapsed_seconds) : null,
        (t.analysts || []).length ? (t.analysts || []).join(' + ') : null,
      ].filter(Boolean).map(C.escapeHtml).join(' · ') + '</p>';

    let group = null;
    const steps = (t.steps || []).map((st, i) => {
      const gl = C.t('tg.' + st.group) === 'tg.' + st.group ? st.group_label : C.t('tg.' + st.group);
      const gh = gl !== group
        ? (group = gl, '<p class="trace-group">' + C.escapeHtml(group) + '</p>')
        : '';
      return gh +
        '<button class="trace-step" type="button" aria-expanded="false" ' +
          'aria-controls="trace-body-' + i + '">' +
          '<span class="trace-num">' + C.escapeHtml(st.order) + '</span>' +
          '<span class="trace-label">' + (C.t('tk.' + st.key) === 'tk.' + st.key ? C.escapeHtml(st.label) : C.t('tk.' + st.key)) + '</span>' +
          '<span class="trace-size">' + (Math.round(st.chars / 100) / 10) + 'k</span>' +
          C.CHEVRON +
        '</button>' +
        '<div class="trace-body md" id="trace-body-' + i + '" hidden>' + C.md(st.markdown || '') +
          (st.truncated ? '<p class="body-sm body-muted">' + C.t('rs.truncated') + '</p>' : '') + '</div>';
    }).join('');
    const failed = job && ['FAILED', 'TIMED_OUT', 'CLAUDE_LIMIT_REACHED', 'CANCELLED'].includes(job.state);
    const result = job?.assistant_result;
    const review = result?.review;
    const reviewHtml = review ? '<p class="body-sm">' + C.escapeHtml([review.account_label,
      review.snapshot_at ? C.dateTime(review.snapshot_at) : null, review.analysed_at ? C.dateTime(review.analysed_at) : null].filter(Boolean).join(' · ')) + '</p>' +
      (review.priorities || []).map(item => '<article class="card"><h3>' + C.escapeHtml(item.symbol) + '</h3><p>' +
        C.escapeHtml(item.recommendation) + '</p><p>' + C.escapeHtml(item.rationale) + '</p><p class="body-sm">' +
        C.escapeHtml((item.missing || []).join('; ')) + '</p></article>').join('') +
      '<p class="body-sm">' + C.escapeHtml((review.limitations || []).join('; ')) + '</p>' : '';
    const answer = result?.answer ? '<div class="md">' + C.md(result.answer) + '</div>' : '';
    const failure = failed ? '<p class="body-sm">' + C.escapeHtml(job.error || C.code(job.state)) + '</p>' : '';
    list.innerHTML = failure + reviewHtml + answer + steps || '<p class="body-sm body-muted" style="padding-top:12px;">' +
      C.t(job && ['QUEUED', 'RUNNING'].includes(job.state) ? 'rs.jobRunning' : 'rs.resultMissing') + '</p>';
    traceReading.shown = !!(failure || reviewHtml || answer || steps || t.final_action);

    list.querySelectorAll('.trace-step').forEach((btn) => {
      btn.addEventListener('click', () => {
        const body = document.getElementById(btn.getAttribute('aria-controls'));
        const open = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', String(!open));
        body.hidden = open;
      });
    });
    saveRead();
  }
  document.addEventListener('visibilitychange', renderTrace);
  document.addEventListener('ct:auth', e => {
    if (e.detail?.clearDrafts) { traceEpoch++; traceReading = null; }
    else if (CT_API.writesEnabled) saveRead();
  });
  document.addEventListener('ct:leave', () => { traceEpoch++; traceReading = null; entryURL = null; });

  const progress = document.getElementById('run-progress');

  /* The backend streams the run's own stdout as `log_tail` (service.py:486).
     That log is the agent working — show it, rather than a progress bar that
     would be inventing a percentage nobody reports. */
  function renderRun() {
    const r = D.researchRun;
    if (!r || !CT_API.matchesResearch(r)) { progress.innerHTML = ''; return; }
    const running = r.state === 'RUNNING' || r.state === 'QUEUED';
    const tone = { QUEUED: 'chip-accent', RUNNING: 'chip-accent', COMPLETE: 'chip-ink',
                   FAILED: 'chip-danger', TIMED_OUT: 'chip-danger',
                   CANCELLED: 'chip-warn',
                   CLAUDE_LIMIT_REACHED: 'chip-warn' }[r.state] || 'chip';
    progress.innerHTML =
      '<hr style="margin-block:20px;" />' +
      '<div class="run-head">' +
        '<span class="agent-dot' + (running ? ' is-live' : '') + '"></span>' +
        '<span class="agent-state">' + C.escapeHtml(C.code(r.state)) + '</span>' +
        '<span class="spacer"></span>' +
        '<span class="chip ' + tone + '">' + C.t(running ? 'rs.polling' : 'rs.settled') + '</span>' +
      '</div>' +
      '<p class="body-sm body-muted" style="margin-top:6px;">' +
        '<span class="mono">' + C.escapeHtml(r.run_key) + '</span>' +
        (r.proposal_created ? ' · ' + C.t('rs.producedProposal') : '') + '</p>' +
      '<pre class="logview" id="run-log"></pre>' +
      (r.proposal_created
        ? '<a class="btn btn-green btn-sm" href="approvals.html" style="margin-top:12px;">' + C.t('rs.reviewIt') + '</a>'
        : '') +
      /* A run that is going nowhere otherwise holds a concurrency slot and a
         share of the day's budget until its timeout, which is most of an
         afternoon on a long analysis. */
      (running
        ? '<button class="btn btn-secondary btn-sm" type="button" id="run-cancel" ' +
            'style="margin-top:12px;" data-od-id="cta-cancel-run">' + C.t('rs.cancel') + '</button>'
        : '');
    const log = document.getElementById('run-log');
    log.textContent = r.log_tail || '';
    log.scrollTop = log.scrollHeight;          /* follow the tail */
    const cancel = document.getElementById('run-cancel');
    if (cancel) cancel.addEventListener('click', () => cancelRun(r.run_key, cancel));
  }
  document.addEventListener('ct:data', renderRun);

  /* POST /research/{run_key}/cancel — the server decides whether the state
     still allows it, so a race with a run that just finished reads as a
     conflict rather than as a silent no-op. */
  async function cancelRun(runKey, button) {
    button.disabled = true;
    button.textContent = C.t('rs.cancelling');
    try {
      const res = await CT_API.post('/research/' + encodeURIComponent(runKey) + '/cancel', {});
      if (res.blocked) {
        button.textContent = C.t('rs.writesOff');
        return;
      }
      CT_API.readOnce('researchRun');
      CT_API.readOnce('researchJobs');
      CT_API.readOnce('researchQueue');
    } catch (err) {
      button.disabled = false;
      button.textContent = C.t('rs.cancelFailed', { err: err.message });
    }
  }

  /* POST /research is a write. It spends Claude quota and spawns a run, so it
     obeys the same switch as every other write. */
  document.getElementById('run-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    /* A run spends one of a limited number of analyses for the day, so a
       second click while the first is still in flight costs real budget. */
    const start = document.querySelector('#run-form button[type="submit"]');
    if (start.disabled || starting || !['agentic', 'demo'].includes(filter.scope)) return;
    if (filter.scope === 'agentic' ? bucketSel.value !== 'micro_live' : !['core', 'sandbox'].includes(bucketSel.value)) return;
    starting = true;
    start.disabled = true;
    const body = {
      ticker: document.getElementById('f-ticker').value.trim().toUpperCase(),
      bucket: bucketSel.value,
      instrument_type: document.getElementById('f-instrument').value,
      trade_date: document.getElementById('f-date').value || null,
      trigger: document.getElementById('f-trigger').value,
      scope: filter.scope,
      purpose: $('f-purpose').value || 'candidate',
    };
    const signature = JSON.stringify(body);
    if (!requestDraft || requestDraft.signature !== signature) requestDraft = { signature, id: crypto.randomUUID() };
    body.request_id = requestDraft.id;
    progress.innerHTML = '<hr style="margin-block:20px;" />' +
      '<p class="body-sm body-muted">' + C.t('rs.starting') + '</p>';
    let res;
    try {
      res = await CT_API.post('/research', body);
    } catch (err) {
      progress.innerHTML = '<hr style="margin-block:20px;" />' +
        '<div class="banner"><p>' + C.escapeHtml(C.t('rs.startFailed', { err: err.message })) + '</p></div>';
      start.disabled = false;
      starting = false;
      return;
    }
    if (res.blocked) {
      progress.innerHTML = '<hr style="margin-block:20px;" />' +
        '<div class="banner"><p>' + C.t('rs.writesOff') + '</p></div>';
      start.disabled = false;
      starting = false;
      return;
    }
    start.disabled = false;
    starting = false; requestDraft = null;
    if (body.scope === filter.scope) { CT_API.watchRun(res.run_key); CT_API.readOnce('researchQueue'); }
  });

  let entryURL;
  function routeEntry() {
    if (entryURL === window.location.href) return;
    entryURL = window.location.href;
    const query = new URLSearchParams(window.location.search);
    const next = { scope: scopes.includes(query.get('scope')) ? query.get('scope') : query.has('scope') ? 'unspecified'
      : query.has('account') ? 'personal' : window.CT_DEMO ? 'demo' : 'agentic',
      account: query.get('account') || '', task_type: query.get('task_type') || '', include_all_personal: query.get('include_all_personal') === 'true' };
    if (next.scope !== 'personal') { next.account = ''; next.include_all_personal = false; }
    if (!['full_research', 'analysis', 'portfolio_review'].includes(next.task_type)) next.task_type = '';
    if (JSON.stringify(next) !== JSON.stringify(filter)) {
      traceEpoch++; traceReading = null; traceSheet.close(); CT_API.watchRun(null);
    }
    filter = next; accountDraft = filter.account;
    $('research-account').value = filter.account; $('research-task').value = filter.task_type;
    $('research-all').checked = filter.include_all_personal;
    CT_API.selectResearch(filter);
    render(); renderRun(); paintHint();
    if (query.get('ticker')) document.getElementById('f-ticker').value = query.get('ticker').toUpperCase();
    if (query.get('run') && !window.CT_DEMO) openTrace(query.get('run'));
  }
  document.addEventListener('ct:route', routeEntry);
  routeEntry();
  C.live([], render, ['researchHistory', 'researchQueue', 'researchRun', 'accounts']);
  document.addEventListener('ct:auth', e => { if (e.detail?.clearDrafts) { accounts.clear(); requestDraft = null; } });
});
