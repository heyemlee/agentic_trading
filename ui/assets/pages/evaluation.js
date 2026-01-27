(function () {
  'use strict';
  const C = window.Console, D = C.D;
  const $ = (id) => document.getElementById(id);
  C.mount('evaluation');
  $('content').hidden = false;

  const percent = (value, signed) => value == null ? '—' :
    (signed && Number(value) > 0 ? '+' : '') + C.pct(Number(value), 2);

  function render() {
    const report = D.evaluation;
    const source = CT_API.state.resources.evaluation;
    $('evaluation-loading').hidden = Boolean(report) || Boolean(source && source.error);
    $('evaluation-empty').hidden = Boolean(report) || !(source && source.error);
    $('evaluation-report').hidden = !report;
    if (!report) {
      $('evaluation-error').textContent = source?.error || C.t('common.loading');
      return;
    }

    const declared = report.declared || {};
    const strategy = report.strategy || {};
    const benchmark = report.benchmark || {};
    const decisions = report.decisions || {};
    const execution = report.execution || {};
    $('ev-state').textContent = C.code(report.state);
    $('ev-days').textContent = C.t('evl.days', { n: report.days_recorded });
    $('ev-period').textContent = C.t('evl.started', { d: report.started_on || '—', n: declared.minimum_days_for_judgement || '—' });
    $('ev-strategy-return').textContent = percent(strategy.return_pct, true);
    $('ev-strategy-dd').textContent = percent(strategy.max_drawdown_pct, false);
    $('ev-strategy-equity').textContent = strategy.equity_usd == null ? '—' : C.usd(strategy.equity_usd);
    $('ev-benchmark-return').textContent = percent(benchmark.return_pct, true);
    $('ev-benchmark-dd').textContent = percent(benchmark.max_drawdown_pct, false);
    $('ev-benchmark-mix').textContent = Object.entries(benchmark.weights_pct || declared.benchmark_weights_pct || {})
      .map(([symbol, weight]) => symbol + ' ' + weight + '%').join(' · ') || '—';
    $('ev-lead').textContent = percent(report.strategy_minus_benchmark_pct_points, true);
    $('ev-interpretation').textContent = C.t(report.conclusive ? 'evl.conclusive' : report.readable ? 'evl.readable' : 'evl.tooEarly');

    $('ev-decisions').innerHTML = [
      C.listRow({ title: C.t('evl.researchRuns'), value: String(decisions.research_runs ?? 0), sub: C.t('evl.outcomes', { v: Object.entries(decisions.run_outcomes || {}).map(([k, v]) => k + ' ' + v).join(' · ') || '—' }) }),
      C.listRow({ title: C.t('evl.dataQuality'), value: percent(decisions.incomplete_data_rate_pct, false), sub: C.t('evl.llmCalls', { n: decisions.llm_calls ?? 0 }) }),
      C.listRow({ title: C.t('evl.execution'), value: C.t('evl.fills', { n: execution.fills ?? 0 }), sub: C.t('evl.traded', { v: C.usd(execution.traded_usd || 0) }) }),
    ].join('');

    const triggered = report.stop_rules_triggered || [];
    $('ev-integrity').innerHTML = [
      C.listRow({ title: C.t('evl.config'), value: C.t(report.config_drifted ? 'evl.changed' : 'evl.unchanged') }),
      C.listRow({ title: C.t('evl.stopRules'), value: triggered.length ? C.t('evl.triggered', { n: triggered.length }) : C.t('evl.noneTriggered'), sub: triggered.join(' · ') }),
    ].join('');
    $('ev-caveat').textContent = report.caveat || '';
  }

  const refresh = () => { render(); C.paintNavStatus(); C.paintConnection(); };
  document.addEventListener('ct:data', refresh);
  document.addEventListener('ct:lang', render);
  render();
  CT_API.start(['health', 'evaluation']);
})();
