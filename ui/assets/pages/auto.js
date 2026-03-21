/* Automatic trading: the plan, the simulated account, and the real one.

   One page, two accounts, chosen by ?scope=paper|live so each has an address. Everything the
   engine decided arrives as a sentence in both languages and is shown as written (C.said); this
   page adds only its own labels, through i18n. It computes nothing about money: every figure,
   every condition and every "why not" is the service's.

   What it may send without a password is what the service accepts without one: a preview,
   anything about the simulated account, and stopping. Starting on the real account, resuming
   there, replacing its strategy and letting a validated one take over by itself all open the
   sheet that asks for the console password, and say first what is about to happen. */
window.Console.page('auto', function (document) {
  'use strict';
  const C = window.Console, D = C.D, h = C.escapeHtml, t = C.t;
  const $ = (id) => document.getElementById(id);
  C.mount('auto');

  let scope = 'paper', mode = 'steady', preview = null, busy = false, armed = null;
  let sheet = null, compared = null, looked = null;

  const money = (v) => (v == null ? '—' : C.usd(Number(v)));
  const signed = (v) => (v == null ? '—' : (Number(v) > 0 ? '+' : '') + C.usd(Number(v)));
  const percent = (v, plus) => (v == null ? '—' : (plus && Number(v) > 0 ? '+' : '') + C.pct(Number(v), 2));
  const row = (title, value, sub) => C.listRow({ title, value: value == null ? '' : String(value), sub });
  const status = () => D.auto || null;
  const sim = () => status()?.simulation || null;
  const real = () => D.autoLive || null;
  const view = () => (scope === 'live' ? real() : sim());
  const modeRow = (key) => (status()?.modes || []).find((m) => m.key === key);
  const modeLabel = (key) => C.said(modeRow(key)?.label) || key;
  const say = (id, text) => { const el = $(id); if (el && el.textContent !== text) el.textContent = text; };

  function strategyLine(s) {
    if (!s) return '';
    return t('auto.strategyLine', { n: s.max_positions, pct: s.position_pct, conf: s.min_confidence,
      stop: s.stop_loss_pct, days: s.max_holding_days }) +
      (s.trend_window ? ' ' + t('auto.strategyTrend', { w: s.trend_window }) : '') +
      (s.take_profit_pct ? ' ' + t('auto.strategyTarget', { pct: s.take_profit_pct }) : '');
  }
  const suggestedBy = (s) => t(s?.suggested_by === 'model' ? 'auto.byModel'
    : s?.suggested_by === 'adjustment' ? 'auto.byAdjustment' : 'auto.byTemplate');

  /* ── which account ──────────────────────────────────────────────── */
  function routeEntry() {
    scope = new URLSearchParams(location.search).get('scope') === 'live' ? 'live' : 'paper';
    armed = null;
    render();
  }
  function renderScopes() {
    C.setHTML($('auto-scopes'), ['paper', 'live'].map((value) =>
      '<a class="chip' + (scope === value ? ' chip-accent' : '') + '" href="auto.html?scope=' + value +
      (window.CT_DEMO ? '&amp;demo=1' : '') + '"' + (scope === value ? ' aria-current="page"' : '') +
      ' data-od-id="auto-scope-' + value + '">' + h(t('auto.scope.' + value)) + '</a>').join(''));
    say('auto-scope-note', t('auto.scopeNote.' + scope));
  }

  /* ── what is in force ───────────────────────────────────────────── */
  function recordRows(v) {
    const plan = v.plan, record = v.record, mark = scope === 'live' ? v.account_mark : v.account;
    const envelope = plan.envelope;
    return [
      row(t('auto.capitalRow'), money(envelope.capital_usd)),
      row(t('auto.deployable'), money(plan.deployable_usd), t('auto.deployableNote')),
      row(t('auto.equity'), money(mark?.equity_usd), mark?.at ? t('auto.asOf', { at: C.dateTime(mark.at) }) : t('auto.noPassYet')),
      /* Made or lost since the record began, with money put in taken out of it. Where the book
         has no funding on record it is measured from the first pass, and says so. */
      row(t('auto.result'), record ? signed(record.result_usd) : '—',
        record ? t(record.measured_from === 'first_mark' ? 'auto.sinceFirstMark' : 'auto.sinceFunding', { d: record.from }) : ''),
      row(t('auto.return'), percent(record?.net_return_pct, true),
        record ? t('auto.sessionsRecorded', { n: record.sessions }) : t('auto.noRecordYet')),
      row(t('auto.drawdown'), percent(record?.max_drawdown_pct, false)),
      row(t('auto.lossLineRow'), money(envelope.loss_pause_usd), t('auto.lossLineNote', { pct: envelope.loss_pause_pct })),
    ].join('');
  }
  function renderCard() {
    const v = view();
    if (!v) {
      C.setHTML($('auto-card'), '<div class="empty"><p class="empty-title">' + h(t('common.loading')) + '</p></div>');
      return;
    }
    if (scope === 'paper' && !v.exists) {
      C.setHTML($('auto-card'), '<div class="empty"><p class="empty-title">' + h(t('auto.notStarted')) +
        '</p><p>' + h(C.said(v.note)) + '</p></div>');
      return;
    }
    const plan = v.plan, state = v.state || {};
    if (!plan) {
      const waiting = scope === 'live' ? v.request : null;
      const ended = scope === 'live' ? v.ended : null;
      C.setHTML($('auto-card'), '<div class="card-tinted"><p class="eyebrow">' + h(t('auto.scope.' + scope)) + '</p>' +
        '<h2 style="margin-top:4px;">' + h(t(waiting ? 'auto.waitingTitle' : 'auto.noPlan')) + '</h2>' +
        (waiting ? '<p class="body-sm" style="margin-top:8px;">' + h(t('auto.waitingBody', {
          until: C.dateTime(waiting.expires_at), n: (waiting.waiting_for || []).length })) + '</p>' +
          (waiting.last_reason ? '<p class="body-sm body-muted" style="margin-top:6px;">' + h(waiting.last_reason) + '</p>' : '')
          : '<p class="body-sm body-muted" style="margin-top:8px;">' +
            h(ended?.withdrawn_reason ? t('auto.endedBecause', { why: ended.withdrawn_reason }) : t('auto.noPlanBody.' + scope)) + '</p>') +
        '</div>');
      return;
    }
    const strategy = plan.strategy;
    const sessions = v.sessions;
    C.setHTML($('auto-card'), '<article class="card-tinted">' +
      '<div class="row-between row-wrap"><div><p class="eyebrow">' + h(modeLabel(plan.envelope.mode) + ' · ' + t('auto.scope.' + scope)) + '</p>' +
      '<h2 style="margin-top:4px;">' + h(t(state.paused ? 'auto.paused' : 'auto.running')) + '</h2></div>' +
      (sessions ? '<span class="chip">' + h(t('auto.sessionsChip', { n: sessions.counted, need: sessions.needed_to_open })) + '</span>'
        : '<span class="chip mono">' + h(plan.opening_version) + '</span>') + '</div>' +
      (state.paused ? '<p class="banner" style="margin-top:12px;">' + h(t(state.kind === 'loss_line' ? 'auto.pausedAtLine' : 'auto.pausedByHand') +
        (state.reason ? ' ' + state.reason : '')) + '</p>' : '') +
      '<p class="body-sm" style="margin-top:12px;">' + h(strategyLine(strategy)) + '</p>' +
      '<p class="body-sm body-muted" style="margin-top:6px;">' + h(suggestedBy(strategy) +
        (C.said(strategy.rationale) ? ' ' + C.said(strategy.rationale) : '')) + '</p>' +
      '<p class="body-sm body-muted" style="margin-top:6px;">' + h(t('auto.mayBuy', { list: (strategy.universe || []).join(' · ') })) + '</p>' +
      '<div class="list" style="margin-top:12px;">' + recordRows(v) + '</div></article>');
  }
  function button(act, label, kind) {
    const arming = armed === act;
    return '<button class="btn ' + (kind || 'btn-secondary') + ' btn-sm" type="button" data-auto-act="' + act + '"' +
      (busy ? ' disabled' : '') + ' data-od-id="auto-' + act + '">' + h(arming ? t('auto.tapAgain') : label) + '</button>';
  }
  function renderActions() {
    const v = view();
    const plan = v?.plan, state = v?.state || {};
    const out = [];
    if (plan && !state.paused) out.push(button('pause', t('auto.pause')));
    if (plan && state.paused) out.push(button('resume', t(scope === 'live' ? 'auto.resumeLive' : 'auto.resume'), 'btn-green'));
    if (plan) out.push(button('stop', t('auto.stop')));
    if (!plan && scope === 'live' && v?.request) out.push(button('withdraw', t('auto.withdraw')));
    C.setHTML($('auto-actions'), out.join(''));
  }
  function renderDetail() {
    const v = view();
    if (!v || (scope === 'paper' && !v.exists)) { C.setHTML($('auto-detail'), ''); return; }
    const parts = [];
    const held = v.positions || [];
    if (held.length) {
      parts.push('<div class="section-head"><h2>' + h(t('auto.holding')) + '</h2></div><div class="list">' +
        held.map((p) => row(p.symbol, p.managed_quantity == null ? t('auto.cannotConfirm') : String(p.managed_quantity),
          p.managed_quantity == null ? (p.reason || '') : t('auto.heldUnder', { v: p.strategy_version || '—' }))).join('') + '</div>');
    }
    const sessions = v.sessions;
    if (sessions && sessions.not_counted?.length) {
      parts.push('<div class="section-head"><h2>' + h(t('auto.notCounted')) + '</h2></div><div class="list">' +
        sessions.not_counted.map((s) => row(s.session, t('auto.notCountedValue'),
          (s.problems || []).map((code) => t('auto.problem.' + code)).join(' · ') ||
            t('auto.notWatched', { n: s.open_passes || 0 }))).join('') + '</div>');
    }
    const coverage = scope === 'paper' ? C.said(v.research?.text) : '';
    if (coverage) parts.push('<p class="banner">' + h(coverage) + '</p>');
    if (v.plan?.managing?.length) {
      parts.push('<p class="body-sm body-muted">' + h(t('auto.managing', { list: v.plan.managing.join(' · ') })) + '</p>');
    }
    if (scope === 'live' && v.last_pass) {
      parts.push('<p class="body-sm body-muted">' + h(t(v.last_pass.error ? 'auto.lastPassFailed' : 'auto.lastPass',
        { at: C.dateTime(v.last_pass.at), why: v.last_pass.error || '' })) + '</p>');
    }
    C.setHTML($('auto-detail'), parts.join(''));
  }

  /* ── the real account: every condition, and what the plan would change ── */
  function changeRows(plan) {
    const waits = Object.fromEntries((plan.landing?.changes || []).map((c) => [c.path, c.waits]));
    /* A switch reads as on or off; any other value is shown as the configuration holds it. */
    const shown = (value) => (value === true ? t('auto.valueOn') : value === false ? t('auto.valueOff')
      : Array.isArray(value) ? value.join(', ') : String(value));
    return (plan.changes || []).map((c) => C.listRow({
      title: t('auto.kind.' + c.kind),
      value: shown(c.to), note: waits[c.path] ? t('auto.waits24') : '',
      sub: C.said(c.why) + ' ' + c.path })).join('');
  }
  function renderGates() {
    const v = real();
    const show = scope === 'live' && !!v;
    $('auto-gates-section').hidden = !show;
    if (!show) return;
    const candidate = v.candidate || {};
    if (!candidate.available) {
      say('auto-gates-lead', C.said(candidate.why));
      C.setHTML($('auto-gates'), ''); C.setHTML($('auto-advisories'), '');
      C.setHTML($('auto-changes'), ''); C.setHTML($('auto-live-actions'), '');
      return;
    }
    const plan = candidate.plan;
    say('auto-gates-lead', t('auto.gatesLead', { mode: modeLabel(plan.envelope.mode),
      capital: money(plan.envelope.capital_usd), version: candidate.version_id }));
    C.setHTML($('auto-gates'), candidate.gates.map((g) => C.gateRow(
      { name: t('auto.waitsOn.' + g.waits_on), detail: C.said(g.text) }, g.ok ? 'is-pass' : 'is-fail')).join(''));
    C.setHTML($('auto-advisories'), (candidate.advisories || []).map((a) =>
      '<p class="banner">' + h(C.said(a.text)) + '</p>').join(''));
    C.setHTML($('auto-changes'), (plan.changes || []).length
      ? '<p class="eyebrow">' + h(t('auto.changesTitle')) + '</p><div class="list" style="margin-top:8px;">' + changeRows(plan) +
        '</div><p class="body-sm body-muted" style="margin-top:8px;">' + h(C.said(plan.landing?.summary)) + '</p>' : '');
    const inForce = v.plan && v.plan.opening_version === candidate.version_id;
    const can = candidate.may_confirm && !v.request && !inForce && !busy;
    C.setHTML($('auto-live-actions'), inForce ? '<p class="body-sm body-muted">' + h(t('auto.alreadyInForce')) + '</p>'
      : v.request ? '<p class="body-sm body-muted">' + h(t('auto.requestStands')) + '</p>'
      : '<button class="btn btn-green" type="button" data-auto-act="start-live"' + (can ? '' : ' disabled') +
        ' data-od-id="cta-auto-start-live">' + h(t('auto.startLive')) + '</button>' +
        (candidate.may_confirm ? '' : '<p class="body-sm body-muted">' + h(t('auto.notYet')) + '</p>'));
  }

  /* ── the real account: a simulated strategy against the one in force ── */
  function lineRows(lines) {
    return (lines || []).map((g) => C.gateRow({ name: t('auto.waitsOn.' + g.waits_on), detail: C.said(g.text) },
      g.ok ? 'is-pass' : 'is-fail')).join('');
  }
  function renderReplacement() {
    const v = real();
    const show = scope === 'live' && !!v?.plan;
    $('auto-replace-section').hidden = !show;
    if (!show) return;
    const info = v.replacement || {};
    const last = looked || info.last;
    C.setHTML($('auto-replace-state'), '<div class="list">' +
      row(t('auto.replaceMode'), t(info.automatic ? 'auto.replaceAuto' : 'auto.replaceAsk'),
        t(info.automatic ? 'auto.replaceAutoNote' : 'auto.replaceAskNote')) +
      row(t('auto.replaceLast'), last ? t('auto.outcome.' + last.outcome) : t('auto.replaceNever'),
        last?.at ? C.dateTime(last.at) : '') + '</div>');
    C.setHTML($('auto-replace-actions'),
      button('evaluate', t('auto.evaluate')) +
      (last?.outcome === 'passed' ? button('replace', t('auto.replaceNow'), 'btn-green') : '') +
      button(info.automatic ? 'auto-replace-off' : 'auto-replace-on', t(info.automatic ? 'auto.replaceTurnOff' : 'auto.replaceTurnOn')));
    C.setHTML($('auto-replace-lines'), lineRows(last?.lines));
  }

  /* ── against doing something simpler (asked for, never polled) ──── */
  function renderPerformance() {
    const v = sim();
    const show = scope === 'paper' && !!v?.exists;
    $('auto-performance-section').hidden = !show;
    if (!show || !compared) { if (show) C.setHTML($('auto-performance'), ''); return; }
    const s = compared.simulation;
    if (!s) { C.setHTML($('auto-performance'), ''); return; }
    const order = ['strategy', 'passive', 'simple_rule', 'benchmark', 'cash'];
    const rows = order.filter((name) => s.rows?.[name]).map((name) => {
      const item = s.rows[name], f = item.figures;
      return C.listRow({ title: t('auto.row.' + name),
        value: f ? percent(f.net_return_pct, true) : t('auto.noResult'),
        note: f ? t('auto.ddShort', { v: percent(f.max_drawdown_pct, false) }) : '',
        sub: C.said(item.says) + (item.why_not ? ' ' + C.said(item.why_not) : '') });
    }).join('');
    C.setHTML($('auto-performance'),
      (rows ? '<div class="list">' + rows + '</div>' : '') +
      (s.comparators_unavailable ? '<p class="banner" style="margin-top:12px;">' + h(s.comparators_unavailable) + '</p>' : '') +
      '<p class="banner" style="margin-top:12px;">' + h(C.said(s.caveat)) + '</p>' +
      '<p class="body-sm body-muted" style="margin-top:8px;">' + h(C.said(compared.note)) + '</p>');
  }

  /* ── every mode, and why each is or is not open ─────────────────── */
  function renderModes() {
    const all = [...(status()?.modes || []), ...(status()?.extensions || [])];
    C.setHTML($('auto-modes'), all.map((m) => {
      const open = m.open_for_real_money;
      const chip = open ? '<span class="chip chip-accent">' + h(t('auto.modeOpen')) + '</span>'
        : '<span class="chip">' + h(t(m.can_simulate ? 'auto.modeSimOnly' : 'auto.modeClosed')) + '</span>';
      return '<article class="card-tinted" data-od-id="auto-mode-' + h(m.key) + '">' +
        '<div class="row-between row-wrap"><h3>' + h(C.said(m.label)) + '</h3>' + chip + '</div>' +
        (m.runs_as ? '<p class="body-sm" style="margin-top:8px;">' + h(C.said(m.runs_as)) + '</p>' : '') +
        (m.simulated_days != null ? '<p class="body-sm body-muted" style="margin-top:6px;">' +
          h(t('auto.modeDays', { n: m.simulated_days })) + '</p>' : '') +
        ((m.blockers || []).length ? '<p class="eyebrow" style="margin-top:12px;">' + h(t('auto.whyNotOpen')) + '</p><ul class="auto-reasons">' +
          m.blockers.map((b) => '<li class="body-sm">' + h(C.said(b.text)) + '</li>').join('') + '</ul>' : '') +
        '</article>';
    }).join(''));
  }

  /* ── a plan, previewed ──────────────────────────────────────────── */
  function renderModeChoices() {
    C.setHTML($('auto-mode-choices'), ['steady', 'swing', 'intraday'].map((key) => {
      const known = modeRow(key);
      const off = known ? !known.can_simulate : key === 'intraday';
      return '<button class="chip' + (mode === key ? ' chip-accent' : '') + '" type="button" role="radio" aria-checked="' +
        (mode === key) + '" data-auto-mode="' + key + '"' + (off ? ' disabled title="' + h(t('auto.modeNotBuilt')) + '"' : '') + '>' +
        h(modeLabel(key)) + '</button>';
    }).join(''));
  }
  function renderPlan() {
    renderModeChoices();
    if (!preview) { C.setHTML($('auto-plan'), ''); return; }
    const p = preview, under = p.under_plan, today = p.today;
    const steps = (under.path || []).map((s) => row(s.symbol, s.feasible ? money(s.order_usd) : t('auto.noOrder'),
      s.feasible ? '' : C.said(s.why_not))).join('');
    const clamps = (p.clamps || []).map((c) => '<li class="body-sm">' + h(C.said(c.why)) + '</li>').join('');
    C.setHTML($('auto-plan'), '<article class="card-tinted" data-od-id="auto-plan-card">' +
      '<p class="eyebrow">' + h(C.said(p.mode?.label) + ' · ' + p.plan_id) + '</p>' +
      '<p style="margin-top:8px;">' + h(C.said(p.summary)) + '</p>' +
      '<p class="body-sm" style="margin-top:12px;">' + h(strategyLine(p.strategy)) + '</p>' +
      '<p class="body-sm body-muted" style="margin-top:6px;">' + h(suggestedBy(p.strategy) +
        (C.said(p.strategy.rationale) ? ' ' + C.said(p.strategy.rationale) : '')) + '</p>' +
      (clamps ? '<ul class="auto-reasons">' + clamps + '</ul>' : '') +
      '<div class="list" style="margin-top:12px;">' +
        row(t('auto.deployToday'), money(today.deployable_usd), t('auto.positionsN', { n: today.positions })) +
        row(t('auto.deployPlan'), money(under.deployable_usd), t('auto.positionsN', { n: under.positions })) +
        steps + '</div>' +
      ((p.changes || []).length ? '<p class="eyebrow" style="margin-top:16px;">' + h(t('auto.changesTitle')) + '</p>' +
        '<div class="list" style="margin-top:8px;">' + changeRows(p) + '</div>' +
        '<p class="body-sm body-muted" style="margin-top:8px;">' + h(C.said(p.landing?.summary)) + '</p>' : '') +
      '<p class="banner" style="margin-top:12px;">' + h(C.said(p.notes?.loss_line)) + '</p>' +
      '<p class="body-sm body-muted" style="margin-top:8px;">' + h(t('auto.simChangesNothing')) + '</p>' +
      '<div class="row row-wrap" style="gap:8px;margin-top:12px;">' +
        '<button class="btn btn-green" type="button" data-auto-act="start-paper"' + (busy || !under.any_order ? ' disabled' : '') +
        ' data-od-id="cta-auto-start-paper">' + h(t(sim()?.plan ? 'auto.switchPaper' : 'auto.startPaper')) + '</button></div>' +
      '</article>');
  }

  function render() {
    renderScopes();
    const stale = !window.CT_DEMO && !!status() && !CT_API.isLive('auto');
    $('auto-stale').hidden = !stale;
    if (stale) say('auto-stale', t('auto.stale'));
    renderCard(); renderActions(); renderDetail(); renderGates(); renderReplacement();
    renderPerformance(); renderModes(); renderPlan();
  }

  /* ── acting ─────────────────────────────────────────────────────── */
  async function refresh() {
    await Promise.all([CT_API.readOnce('auto'), CT_API.readOnce('autoLive')]);
  }
  async function send(path, body, statusId) {
    if (busy) return null;
    busy = true; armed = null; render();
    say(statusId, t('auto.sending'));
    try {
      const answer = await CT_API.post(path, body);
      if (answer?.blocked) { say(statusId, t('auto.demoBlocked')); return null; }
      await refresh();
      return answer;
    } catch (error) {
      say(statusId, (error.notSent ? t('auto.notSent') + ' ' : '') + error.message);
      return null;
    } finally { busy = false; render(); }
  }
  function ask(act, title, lead, note, confirm) {
    sheet = act;
    say('auto-sheet-title', title); say('auto-sheet-lead', lead); say('auto-sheet-note', note || '');
    say('auto-sheet-confirm', confirm); say('auto-sheet-status', '');
    $('auto-password').value = '';
    C.openSheet('auto-sheet');
  }
  const envelopeOf = () => {
    const capital = Number($('auto-capital').value), loss = Number($('auto-loss').value);
    return { mode, ...(capital > 0 ? { capital_usd: capital } : {}), ...(loss > 0 ? { loss_pause_pct: loss } : {}) };
  };
  const twoTaps = ['pause', 'stop', 'withdraw', 'auto-replace-off'];

  async function act(name) {
    if (twoTaps.includes(name) && armed !== name) { armed = name; render(); return; }
    if (name === 'preview') {
      if (window.CT_DEMO) { preview = D.autoPlanExample || null; say('auto-plan-status', t('auto.demoPreview')); render(); return; }
      const wanted = envelopeOf();
      const answer = await send('/auto-trading/plan', wanted, 'auto-plan-status');
      if (answer) { preview = { ...answer, asked: wanted }; say('auto-plan-status', ''); render(); }
    } else if (name === 'start-paper' && preview) {
      const answer = await send('/auto-trading/start', { ...(preview.asked || { mode: preview.envelope.mode }),
        plan_id: preview.plan_id, strategy: preview.strategy, scope: 'paper' }, 'auto-plan-status');
      if (answer) { preview = null; say('auto-plan-status', t('auto.startedPaper')); render(); }
    } else if (name === 'pause') {
      if (await send('/auto-trading/pause', { reason: '', scope }, 'auto-result')) say('auto-result', t('auto.pausedDone'));
    } else if (name === 'stop') {
      if (await send('/auto-trading/stop', { reason: '', scope }, 'auto-result')) say('auto-result', t('auto.stoppedDone'));
    } else if (name === 'withdraw') {
      if (await send('/auto-trading/live/withdraw', { reason: '' }, 'auto-result')) say('auto-result', t('auto.withdrawnDone'));
    } else if (name === 'resume' && scope === 'paper') {
      const answer = await send('/auto-trading/resume', { reason: '', scope }, 'auto-result');
      if (answer) say('auto-result', resumedText(answer));
    } else if (name === 'resume') {
      const state = real()?.state || {};
      ask('resume', t('auto.resumeTitle'), t(state.kind === 'loss_line' ? 'auto.resumeLineLead' : 'auto.resumeLead'),
        state.reason || '', t('auto.resumeLive'));
    } else if (name === 'start-live') {
      const candidate = real()?.candidate;
      if (!candidate?.available) return;
      const envelope = candidate.plan.envelope;
      ask('start-live', t('auto.startLiveTitle'),
        t('auto.startLiveLead', { mode: modeLabel(envelope.mode), capital: money(envelope.capital_usd), line: money(envelope.loss_pause_usd) }),
        t(candidate.open ? 'auto.startLiveNow' : 'auto.startLiveWaits', { n: (candidate.plan.changes || []).length }),
        t('auto.startLiveConfirm'));
    } else if (name === 'replace') {
      ask('replace', t('auto.replaceSheetTitle'), t('auto.replaceSheetLead'), '', t('auto.replaceNow'));
    } else if (name === 'auto-replace-on') {
      ask('auto-replace-on', t('auto.replaceTurnOn'), t('auto.replaceOnLead'), '', t('auto.replaceTurnOn'));
    } else if (name === 'auto-replace-off') {
      if (await send('/auto-trading/replacement', { on: false }, 'auto-result')) say('auto-result', t('auto.replaceOffDone'));
    } else if (name === 'evaluate') {
      const answer = await send('/auto-trading/candidate/evaluate', {}, 'auto-result');
      if (answer) { looked = answer; say('auto-result', t('auto.outcome.' + answer.outcome)); render(); }
    } else if (name === 'performance') {
      if (window.CT_DEMO) { compared = D.autoPerformanceExample || null; render(); return; }
      say('auto-performance-status', t('auto.sending'));
      try { compared = await CT_API.fetchJSON('/auto-trading/performance'); say('auto-performance-status', ''); }
      catch (error) { say('auto-performance-status', error.message); }
      render();
    }
  }
  const resumedText = (answer) => {
    const moved = answer.loss_line_moved;
    return moved ? t('auto.resumedMoved', { from: signed(moved.from), to: signed(moved.to) }) : t('auto.resumedDone');
  };
  async function confirmSheet() {
    const password = $('auto-password').value;
    if (!password) { say('auto-sheet-status', t('auto.passwordNeeded')); return; }
    const name = sheet;
    let path, body, done;
    if (name === 'start-live') {
      const candidate = real()?.candidate;
      if (!candidate?.available) return;
      path = '/auto-trading/start'; body = { ...candidate.inputs, scope: 'live', password };
      done = (answer) => t(answer.started ? 'auto.startedLive' : 'auto.requestedLive');
    } else if (name === 'resume') {
      path = '/auto-trading/resume'; body = { reason: '', scope: 'live', password }; done = resumedText;
    } else if (name === 'replace') {
      path = '/auto-trading/candidate/replace'; body = { password };
      done = (answer) => { looked = answer; return t('auto.outcome.' + answer.outcome); };
    } else if (name === 'auto-replace-on') {
      path = '/auto-trading/replacement'; body = { on: true, password }; done = () => t('auto.replaceOnDone');
    } else return;
    $('auto-sheet-confirm').disabled = true;
    const answer = await send(path, body, 'auto-sheet-status');
    $('auto-sheet-confirm').disabled = false;
    $('auto-password').value = '';
    if (!answer) return;
    C.closeSheet('auto-sheet');
    say('auto-result', done(answer));
    render();
  }

  document.addEventListener('click', (event) => {
    const choice = event.target.closest?.('[data-auto-mode]');
    if (choice && !choice.disabled) { mode = choice.dataset.autoMode; preview = null; render(); return; }
    const control = event.target.closest?.('[data-auto-act]');
    if (control && !control.disabled) { act(control.dataset.autoAct); return; }
    if (armed) { armed = null; render(); }
  });
  $('auto-sheet-confirm').addEventListener('click', confirmSheet);
  $('auto-sheet-cancel').addEventListener('click', () => C.closeSheet('auto-sheet'));
  document.addEventListener('ct:route', routeEntry);
  routeEntry();
  C.live(['auto'], render, ['autoLive']);
});
