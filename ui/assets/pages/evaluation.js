window.Console.page('evaluation', function (document) {
  'use strict';
  const C = window.Console, D = C.D;
  const $ = (id) => document.getElementById(id);
  C.mount('evaluation');

  const percent = (value, signed) => value == null ? '—' :
    (signed && Number(value) > 0 ? '+' : '') + C.pct(Number(value), 2);
  const scopes = ['agentic', 'demo', 'route_quality'];
  let scope = null;

  function routeEntry() {
    const requested = new URLSearchParams(location.search).get('scope');
    scope = scopes.includes(requested) ? requested : window.CT_DEMO ? 'demo' : 'agentic';
    CT_API.selectEvaluation(scope);
    render();
  }
  function renderRoutes(report) {
    $('ev-route-period').textContent = C.t('evl.routePeriod', { since: report.since || '—', at: C.dateTime(report.checked_at) });
    const metric = (row, flag, value) => row.evaluated?.[flag] === true && value != null ? String(value) : C.t('evl.notEvaluated');
    C.setHTML($('ev-routes'), (report.routes || []).map(row => {
      const entry = (label, flag, value) => C.listRow({ title: C.t('evl.' + label), value: metric(row, flag, value) });
      return '<article class="card-tinted"><h2>' + C.escapeHtml(row.route) + '</h2><div class="list">' +
        entry('routeRuns', 'failures', row.runs) + entry('routeComplete', 'failures', row.complete) +
        entry('routeFailed', 'failures', row.failed) + entry('routeTimeout', 'failures', row.timed_out) +
        entry('routeLimits', 'failures', row.limit_hits) +
        entry('routeTime', 'timing', row.median_elapsed_seconds == null ? null : C.clock(row.median_elapsed_seconds)) +
        entry('routeCalls', 'calls', row.llm_calls_total) +
        entry('routeEvidence', 'evidence', row.evidence_check_pass_rate_pct == null ? null : percent(row.evidence_check_pass_rate_pct)) +
        entry('routeFormat', 'format', row.format_pass_rate_pct == null ? null : percent(row.format_pass_rate_pct)) +
        entry('routeFacts', 'facts', row.fact_accuracy_pct == null ? null : percent(row.fact_accuracy_pct)) +
        entry('routeEdits', 'edits', row.human_edit_rate_pct == null ? null : percent(row.human_edit_rate_pct)) +
        '</div></article>';
    }).join('') || '<p class="body-muted">' + C.t('evl.noRoutes') + '</p>');
    $('ev-route-note').textContent = report.note || '';
  }

  function renderSample(sample) {
    const gaps = sample?.gaps || [];
    const known = sample?.basis === 'sessions' && sample.stale != null;
    $('ev-sample-status').textContent = C.t(!known ? 'evl.sampleUnknown' : sample.stale ? 'evl.sampleStale' : gaps.length ? 'evl.sampleGaps' : 'evl.sampleCurrent');
    $('ev-sample-detail').textContent = C.t('evl.sampleDetail', { last: sample?.last_snapshot_on || '—', behind: sample?.sessions_behind ?? '—' }) +
      (sample?.basis && sample.basis !== 'sessions' ? ' · ' + sample.basis : '');
    C.setHTML($('ev-gaps'), gaps.map(gap => C.listRow({ title: gap.from + ' → ' + gap.to,
      value: C.t('evl.missingSessions', { n: gap.missing_sessions }), sub: gap.reason })).join(''));
  }

  function render() {
    C.setHTML($('evaluation-scopes'), scopes.map(value => '<a class="chip' + (scope === value ? ' chip-accent' : '') +
      '" href="evaluation.html?scope=' + value + (window.CT_DEMO ? '&amp;demo=1' : '') + '"' +
      (scope === value ? ' aria-current="page"' : '') + '>' + C.t('evl.scope.' + value) + '</a>').join(''));
    $('evaluation-scopes').setAttribute('aria-label', C.t('evl.scopeLabel'));
    $('ev-scope-note').textContent = C.t('evl.scopeNote.' + scope);
    const report = D.evaluation?.scope === scope ? D.evaluation : null;
    const source = CT_API.state.resources.evaluation;
    $('evaluation-empty').hidden = !!report;
    $('evaluation-report').hidden = !report || scope === 'route_quality';
    $('evaluation-routes').hidden = !report || scope !== 'route_quality';
    const stale = report && !window.CT_DEMO && !CT_API.isLive('evaluation');
    $('evaluation-status').hidden = !stale;
    $('evaluation-status').textContent = stale ? C.t('evl.cached') + (source?.error ? ' · ' + source.error : '') : '';
    if (!report) {
      $('evaluation-empty-title').textContent = C.t(source?.error ? 'evl.unavailable' : 'common.loading');
      $('evaluation-error').textContent = source?.error || '';
      return;
    }
    if (scope === 'route_quality') { renderRoutes(report); return; }
    const notStarted = report.state === 'NOT_STARTED';
    $('ev-not-started').hidden = !notStarted;
    $('ev-not-started').textContent = C.t('evl.notStartedBody');
    $('ev-performance').hidden = notStarted;
    $('ev-integrity-section').hidden = notStarted;
    $('ev-sample-section').hidden = notStarted;
    renderSample(report.sample);

    const declared = report.declared || {};
    const strategy = report.strategy || {};
    const benchmark = report.benchmark || {};
    const decisions = report.decisions || {};
    const execution = report.execution || {};
    $('ev-state').textContent = notStarted ? C.t('evl.notStarted') : C.code(report.state);
    $('ev-days').textContent = C.t('evl.days', { n: report.days_recorded });
    $('ev-period').textContent = notStarted ? '' : C.t('evl.started', { d: report.started_on || '—', n: declared.minimum_days_for_judgement || '—' });
    $('ev-strategy-return').textContent = percent(strategy.return_pct, true);
    $('ev-strategy-dd').textContent = percent(strategy.max_drawdown_pct, false);
    $('ev-strategy-equity').textContent = strategy.equity_usd == null ? '—' : C.usd(strategy.equity_usd);
    $('ev-benchmark-return').textContent = percent(benchmark.return_pct, true);
    $('ev-benchmark-dd').textContent = percent(benchmark.max_drawdown_pct, false);
    $('ev-benchmark-mix').textContent = Object.entries(benchmark.weights_pct || declared.benchmark_weights_pct || {})
      .map(([symbol, weight]) => symbol + ' ' + weight + '%').join(' · ') || '—';
    $('ev-lead').textContent = percent(report.strategy_minus_benchmark_pct_points, true);
    $('ev-interpretation').textContent = C.t(report.sample?.stale || report.sample?.gaps?.length ? 'evl.incompleteSample'
      : report.sample?.basis !== 'sessions' || report.sample?.stale == null ? 'evl.unknownSampleInterpretation'
      : report.conclusive ? 'evl.conclusive' : report.readable ? 'evl.readable' : 'evl.tooEarly');

    C.setHTML($('ev-decisions'), [
      C.listRow({ title: C.t('evl.researchRuns'), value: String(decisions.research_runs ?? 0), sub: C.t('evl.outcomes', { v: Object.entries(decisions.run_outcomes || {}).map(([k, v]) => k + ' ' + v).join(' · ') || '—' }) }),
      C.listRow({ title: C.t('evl.dataQuality'), value: percent(decisions.incomplete_data_rate_pct, false), sub: C.t('evl.llmCalls', { n: decisions.llm_calls ?? 0 }) }),
      C.listRow({ title: C.t('evl.execution'), value: C.t('evl.fills', { n: execution.fills ?? 0 }), sub: C.t('evl.traded', { v: C.usd(execution.traded_usd || 0) }) }),
    ].join(''));

    const triggered = report.stop_rules_triggered || [];
    C.setHTML($('ev-integrity'), [
      C.listRow({ title: C.t('evl.config'), value: C.t(report.config_drifted ? 'evl.changed' : 'evl.unchanged') }),
      C.listRow({ title: C.t('evl.stopRules'), value: triggered.length ? C.t('evl.triggered', { n: triggered.length }) : C.t('evl.noneTriggered'), sub: triggered.join(' · ') }),
    ].join(''));
    $('ev-caveat').textContent = report.caveat || '';
  }

  document.addEventListener('ct:route', routeEntry);
  routeEntry();
  C.live([], render, ['evaluation']);
});
