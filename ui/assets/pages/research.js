(function () {
  const C = window.Console, D = C.D;
  C.mount('research');
  C.wireTabs();
  const startSection = document.querySelector('[data-od-id="start-run"]');
  const chartSection = document.querySelector('[data-od-id="confidence-chart"]');
  chartSection.before(startSection);
  const paintScope = () => {
    document.getElementById('run-scope').textContent = C.t('rs.bucket') + ' · ' + C.bucket(document.getElementById('f-bucket').value);
  };
  document.getElementById('f-bucket').addEventListener('change', paintScope);

  /* Everything below is re-run on every poll cycle. */
  function render() {
    paintScope();

    const FLOOR = 50;

    /* Runs on a hidden bucket leave the charts and the lists. The record is
       untouched: `ct-evaluate report` still counts every one of them. */
    const runs = (D.research || []).filter((r) => C.showsBucket(r.bucket));

    /* ── confidence bars — filled encoding, real values only ─────────── */
    document.getElementById('conf-chart').innerHTML = '<div class="stack stack-4">' +
      runs.map((r) =>
        '<div data-od-id="conf-' + C.escapeHtml(r.run_id) + '">' +
          '<div class="row-between" style="margin-bottom:6px;">' +
            '<span class="body-sm">' + C.escapeHtml(r.ticker) + ' · ' + C.escapeHtml(C.bucket(r.bucket)) +
              ' <span class="body-muted">' + C.dateOnly(r.trade_date) + '</span></span>' +
            '<span class="body-sm num">' + C.escapeHtml(r.confidence) + '</span>' +
          '</div>' +
          '<div style="position:relative;">' +
            '<div class="meter-track" style="height:10px;">' +
              '<div class="meter-fill' + (r.confidence < FLOOR ? ' is-warn' : ' is-accent') +
              '" style="width:' + Math.max(0, Math.min(100, Number(r.confidence) || 0)) + '%"></div></div>' +
            '<span style="position:absolute;top:-3px;bottom:-3px;left:' + FLOOR +
              '%;width:2px;background:var(--fg);" aria-hidden="true"></span>' +
          '</div>' +
        '</div>').join('') + '</div>';

    /* ── run list ────────────────────────────────────────────────────── */
    function runList(rows) {
      if (!rows.length) {
        return '<div class="empty"><p class="empty-title">' + C.t('rs.noRuns') + '</p>' +
          '<p>' + C.t('rs.noRunsBody') + '</p></div>';
      }
      return '<div class="list">' + rows.map((r) => C.listRow({
        id: 'run-row-' + r.run_id,
        href: '#trace',
        trace: r.run_id,
        title: r.ticker + ' · ' + C.bucket(r.bucket),
        sub: (r.final_action ? C.code(r.final_action) : C.t('ac.incomplete')) + ' · ' +
             C.code(r.portfolio_manager_rating) + ' · ' +
             C.t('rs.traceCalls', { n: r.llm_calls }) + ' · ' + C.clock(r.elapsed_seconds),
        value: String(r.confidence),
        note: C.dateOnly(r.trade_date),
      })).join('') + '</div>';
    }
    document.getElementById('panel-runs-all').innerHTML = runList(runs);
    document.getElementById('panel-runs-notrade').innerHTML =
      runList(runs.filter((r) => r.final_action === 'NO_TRADE'));
    document.getElementById('panel-runs-proposal').innerHTML =
      runList(runs.filter((r) => r.proposal_id));

    /* Most recent decision — read from the record, not baked into markup. */
    const last = runs[0];
    document.getElementById('recent-card').innerHTML = !last ? '' :
      '<div class="section-head">' +
        '<span class="chip chip-ink">' +
          (last.final_action ? C.escapeHtml(C.code(last.final_action)) : C.t('ac.incomplete')) + '</span>' +
        '<span class="body-sm body-muted mono">' + C.escapeHtml(last.run_id) + '</span></div>' +
      '<h3 style="font-size:20px;">' + C.escapeHtml(last.ticker) + ' · ' + C.escapeHtml(C.bucket(last.bucket)) + ' · ' +
        C.dateOnly(last.trade_date) + '</h3>' +
      '<dl class="kv" style="margin-top:16px;">' +
        '<div class="kv-row"><dt>' + C.t('rs.rating') + '</dt><dd>' +
          C.escapeHtml(C.code(last.portfolio_manager_rating)) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.policyAction') + '</dt><dd>' +
          C.escapeHtml((last.policy_action || '—')) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.hardRiskVerdict') + '</dt><dd>' +
          C.escapeHtml((last.hard_risk_verdict || '—')) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.strength') + '</dt><dd>' +
          C.escapeHtml(last.confidence) + ' / 100</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.callsElapsed') + '</dt><dd>' +
          C.escapeHtml(last.llm_calls) + ' · ' + C.clock(last.elapsed_seconds) + '</dd></div>' +
        '<div class="kv-row"><dt>' + C.t('rs.proposal') + '</dt><dd>' +
          C.escapeHtml((last.proposal_id || C.t('common.none'))) + '</dd></div>' +
      '</dl>';

  }

  C.live(['research', 'researchJobs', 'researchRun'], render);

  /* Reattach to a queued/running job after a refresh or phone reconnect. */
  document.addEventListener('ct:data', () => {
    if (CT_API.activeRun) return;
    const active = (D.researchJobs || []).find((job) =>
      job.state === 'QUEUED' || job.state === 'RUNNING');
    if (active) CT_API.watchRun(active.run_key);
  });

  /* ── POST /research → poll GET /research/{run_key} ───────────────── */
  const hint = document.getElementById('run-hint');
  const bucketSel = document.getElementById('f-bucket');
  /* One switch governs the whole console: a bucket nobody can see is not a
     bucket anyone can start a run against either. */
  Array.from(bucketSel.options).forEach((opt) => {
    if (!C.showsBucket(opt.value)) opt.remove();
  });
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
  document.getElementById('trace-close').addEventListener('click', () => traceSheet.close());
  traceSheet.addEventListener('click', (e) => { if (e.target === traceSheet) traceSheet.close(); });
  document.addEventListener('click', (e) => {
    const row = e.target.closest && e.target.closest('[data-trace]');
    if (!row) return;
    e.preventDefault();
    openTrace(row.getAttribute('data-trace'));
  });

  async function openTrace(runId) {
    const head = document.getElementById('trace-head');
    const list = document.getElementById('trace-steps');
    head.innerHTML = '<p class="agent-state">' + C.escapeHtml(runId) + '</p>';
    list.innerHTML = '<p class="body-sm body-muted" style="padding-top:12px;">' + C.t('rs.traceLoading') + '</p>';
    traceSheet.showModal();

    let t;
    try {
      t = await CT_API.fetchJSON('/research/' + encodeURIComponent(runId) + '/trace');
    } catch (err) {
      list.innerHTML = '<div class="banner" style="margin-top:12px;"><p>' +
        C.t('rs.traceFailed', { err: C.escapeHtml(err.message) }) + '</p></div>';
      return;
    }

    const usage = t.llm_usage || {};
    head.innerHTML =
      '<p class="agent-state">' + C.escapeHtml(t.ticker || runId) + ' · ' +
        (t.final_action ? C.escapeHtml(C.code(t.final_action)) : C.t('home.noResult')) + '</p>' +
      '<p class="agent-note">' + [
        t.bucket ? C.bucket(t.bucket) : null,
        t.trade_date ? C.dateOnly(t.trade_date) : null,
        t.confidence != null ? C.t('rs.traceStrength', { c: t.confidence }) : null,
        usage.calls ? C.t('rs.traceCalls', { n: usage.calls }) : null,
        t.elapsed_seconds ? C.clock(t.elapsed_seconds) : null,
        (t.analysts || []).length ? (t.analysts || []).join(' + ') : null,
      ].filter(Boolean).map(C.escapeHtml).join(' · ') + '</p>';

    let group = null;
    list.innerHTML = (t.steps || []).map((st, i) => {
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
    }).join('') || '<p class="body-sm body-muted" style="padding-top:12px;">' + C.t('rs.traceEmpty') + '</p>';

    list.querySelectorAll('.trace-step').forEach((btn) => {
      btn.addEventListener('click', () => {
        const body = document.getElementById(btn.getAttribute('aria-controls'));
        const open = btn.getAttribute('aria-expanded') === 'true';
        btn.setAttribute('aria-expanded', String(!open));
        body.hidden = open;
      });
    });
  }

  const progress = document.getElementById('run-progress');

  /* The backend streams the run's own stdout as `log_tail` (service.py:486).
     That log is the agent working — show it, rather than a progress bar that
     would be inventing a percentage nobody reports. */
  function renderRun() {
    const r = D.researchRun;
    if (!r) return;
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
    } catch (err) {
      button.disabled = false;
      button.textContent = C.t('rs.cancelFailed', { err: err.message });
    }
  }

  /* POST /research is a write. It spends Claude quota and spawns a run, so it
     obeys the same switch as every other write. */
  document.getElementById('run-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = {
      ticker: document.getElementById('f-ticker').value.trim().toUpperCase(),
      bucket: bucketSel.value,
      instrument_type: document.getElementById('f-instrument').value,
      trade_date: document.getElementById('f-date').value || null,
      trigger: document.getElementById('f-trigger').value,
    };
    progress.innerHTML = '<hr style="margin-block:20px;" />' +
      '<p class="body-sm body-muted">' + C.t('rs.starting') + '</p>';
    let res;
    try {
      res = await CT_API.post('/research', body);
    } catch (err) {
      progress.innerHTML = '<hr style="margin-block:20px;" />' +
        '<div class="banner"><p>' + C.escapeHtml(C.t('rs.startFailed', { err: err.message })) + '</p></div>';
      return;
    }
    if (res.blocked) {
      progress.innerHTML = '<hr style="margin-block:20px;" />' +
        '<div class="banner"><p>' + C.t('rs.writesOff') + '</p></div>';
      return;
    }
    CT_API.watchRun(res.run_key);
  });

  const query = new URLSearchParams(window.location.search);
  if (query.get('ticker')) document.getElementById('f-ticker').value = query.get('ticker').toUpperCase();
  if (query.get('run') && !window.CT_DEMO) openTrace(query.get('run'));
})();
