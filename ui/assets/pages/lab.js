'use strict';
/* ─────────────────────────────────────────────────────────────────────────
 * The research room: one run, drawn as the office that performed it, and what the room
 * studies -- what the owner holds, then what the owner added to their Robinhood watchlists.
 *
 * Everything here is something the engine recorded. Four sources feed the room and
 * none of them is a simulation:
 *
 *   /research/roster       the team, and which analysts an instrument type seats
 *   /research/{key}        a run in flight, with the events its model calls append
 *   /research/{id}/trace   a finished run, with the report each desk wrote
 *   /research/subjects     the owner's holdings and watchlist names, as the weekly run reads them
 *
 * A Demo book's run and a record bound to no account are about nothing the owner holds or
 * chose, so the room draws neither (owner's decision, 2026-09-23).
 *
 * Two rules the drawing must not break. A desk changes only when an event says so, so
 * nothing here is driven by a timer and no percentage appears anywhere: the engine
 * reports discrete events, never progress. And the decision does not belong to the
 * room — the portfolio manager rates, while policy.py and the hard-risk rules outside
 * the wall decide what may happen, which is why the gate panel is not a desk.
 * ───────────────────────────────────────────────────────────────────── */
window.Console.page('lab', function (document) {
  const C = window.Console, D = C.D;
  C.mount('lab');
  window.createLabPixelScene?.(document);
  const q = (s) => document.querySelector(s);
  /* One run's report opens over the room (lab/report.js). */
  const report = window.LabReport(document, C, { record: recordFor, canStart: (ticker) => subjectFor(ticker)?.symbol || null });

  /* Seat order belongs to the drawn room, not to the team: pixel-office.js addresses
     seats by index, and gives seats 0-3, 7 and 8 their own desk furniture. The roster
     says who exists and who runs; this list says where they sit. */
  const SEATS = ['market', 'news', 'social', 'fundamentals', 'bull', 'bear', 'manager',
                 'trader', 'portfolio', 'aggressive', 'neutral', 'conservative'];
  const ZONE = { analysts: 'analysis', research: 'debate', trading: 'trade',
                 portfolio: 'trade', risk: 'risk' };
  const RUNNING = ['QUEUED', 'RUNNING'];
  const BROKEN = ['FAILED', 'TIMED_OUT', 'CANCELLED', 'CLAUDE_LIMIT_REACHED'];
  /* The instrument is the run's, never the reader's. Most jobs are queued as "auto", which
     names none: the engine classifies the symbol when the run starts and says which it
     chose in its start event and its record. Only those answers are an instrument. */
  const INSTRUMENTS = ['etf', 'stock', 'crypto'];
  const instrumentOf = (value) => (INSTRUMENTS.includes(value) ? value : null);
  const HANDOFF = { market: 'bull', news: 'bull', social: 'bull', fundamentals: 'bull',
                    bull: 'bear', bear: 'manager', manager: 'trader', trader: 'neutral',
                    aggressive: 'portfolio', neutral: 'portfolio', conservative: 'portfolio' };

  /* Reader's choices. None of these changes what the engine did. */
  let chosenRun = null, chosenRole = null, cursor = null;
  let ambientEnabled = true, fileTimer = null, epoch = 0;
  const expanded = { held: false, added: false };
  const traces = new Map();
  const walking = new Set();
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const canAnimate = () => ambientEnabled && !reduced.matches && !document.hidden;

  /* "+0800" is not an ISO offset and Safari rejects it, so normalise before parsing. */
  function at(raw) {
    if (!raw) return null;
    const d = new Date(String(raw).replace(/([+-]\d{2})(\d{2})$/, '$1:$2'));
    return isNaN(d.getTime()) ? null : d;
  }
  const clockTime = (raw) => { const d = at(raw); return d ? d.toLocaleTimeString() : '—'; };
  const secondsSince = (raw) => { const d = at(raw); return d ? Math.max(0, Math.round((Date.now() - d) / 1000)) : null; };
  const label = (key, fallback) => (C.t(key) === key ? fallback : C.t(key));

  /* ── what the API is currently saying ─────────────────────────────── */
  const roster = () => D.labRoster;
  /* Only runs about the owner's own money: never a Demo book's, never one bound to nothing. */
  const ownRun = (row) => !['demo', 'unspecified'].includes(row.scope);
  /* Conversations, shortlists, refreshes and portfolio reviews share the queue but run no
     desks, so they have no office to draw. Strategy research and a personal analysis do. */
  const graphJobs = () => (D.researchJobs || []).filter((j) => ownRun(j) && (!j.task || j.task.kind === 'analysis'));
  const history = () => (D.research || []).filter((r) => ownRun(r) && r.task_type !== 'portfolio_review');

  function runList() {
    const records = history(), out = [], seen = new Set();
    graphJobs().forEach((job) => {
      seen.add(job.run_key);
      out.push(entry(job, records.find((r) => r.run_id === job.run_key)));
    });
    records.forEach((r) => { if (!seen.has(r.run_id)) out.push(entry(null, r)); });
    return out;
  }
  function entry(job, record) {
    const source = job || record;
    return {
      key: job ? job.run_key : record.run_id,
      ticker: source.ticker, bucket: source.bucket,
      /* Which account the run was about, so a Demo study is never read as the Agentic one. */
      account: source.account_label || null, scope: source.scope || null,
      accountId: source.account_id || null, accountKind: source.account_scope_kind || null,
      date: job ? job.trade_date : record.trade_date,
      instrument: instrumentOf(job && job.instrument_type) || instrumentOf(record && record.instrument_type),
      state: job ? job.state : 'COMPLETE',
      live: !!job && RUNNING.includes(job.state),
      broken: !!job && BROKEN.includes(job.state),
      error: job ? job.error : null,
      policy: record ? record.policy_action : null,
      confidence: record ? record.confidence : null,
      hardRisk: record ? record.hard_risk_verdict : null,
      finalAction: record ? record.final_action : null,
      proposalId: record ? record.proposal_id : null,
    };
  }
  const selectedRun = () => {
    const runs = runList();
    const found = runs.find((r) => r.key === chosenRun);
    if (found || !chosenRun) return found || runs[0] || null;
    /* A link can name a run older than the list this room reads. That run is drawn, from
       its own trace once it arrives, rather than another one in its place. */
    return { key: chosenRun, ticker: '—', bucket: null, account: null, scope: null, accountId: null,
             accountKind: null, date: null, instrument: null, state: 'COMPLETE', live: false,
             broken: false, error: null, policy: null, confidence: null, hardRisk: null,
             finalAction: null, proposalId: null, named: true };
  };
  const accountOf = (run) => run.account || (run.bucket ? label('bucket.' + run.bucket, run.bucket) : '');

  /* ── what the room studies ────────────────────────────────────────── */
  const subjects = () => D.researchSubjects || null;
  function subjectFor(ticker) {
    const all = subjects();
    const symbol = String(ticker || '').toUpperCase();
    return all ? [...(all.held || []), ...(all.added || [])].find((s) => s.symbol === symbol) || null : null;
  }
  /* The record a report is opened for, wherever the page already holds it. */
  function recordFor(runId) {
    const all = subjects();
    const latest = all ? [...(all.held || []), ...(all.added || [])].map((s) => s.latest).filter(Boolean) : [];
    return (D.research || []).find((r) => r.run_id === runId) || latest.find((r) => r.run_id === runId) || null;
  }

  /* A finished run's reports are read once and kept. Demo mode carries the same shape as
     a fixture, because the room is what the project shows first and an empty office
     would show nothing. */
  function traceFor(key) {
    if (!key) return null;
    if (window.CT_DEMO) return (D.labTrace || {})[key] || null;
    if (!traces.has(key)) {
      traces.set(key, 'loading');
      CT_API.fetchJSON('/research/' + encodeURIComponent(key) + '/trace')
        /* Reports are the desks' own words, so they must be this run's words: a response
           that names a different run is refused rather than drawn on the wrong office. */
        .then((t) => {
          traces.set(key, t && t.run_id && t.run_id !== key
            ? { failed: C.t('lab.traceMismatch') } : t);
          render();
        })
        .catch((e) => { traces.set(key, { failed: e.message }); render(); });
    }
    const held = traces.get(key);
    return held === 'loading' ? null : held;
  }

  /* The live run's own payload, and only if it is still the run being shown: a late
     response must never be attached to a run the reader has since moved away from. */
  function liveRun(run) {
    const payload = D.researchRun;
    return run && run.live && payload && payload.run_key === run.key ? payload : null;
  }

  /* ── the room, derived ────────────────────────────────────────────── */
  function view() {
    const team = roster();
    const roles = (team && team.roles) || [];
    const run = selectedRun();
    const live = liveRun(run);
    const progress = live && live.progress;
    const trace = run && !run.live ? traceFor(run.key) : null;
    const loadedTrace = trace && !trace.failed ? trace : null;
    /* A run named by a link and not in the list takes its facts from its own trace. */
    if (run && run.named && loadedTrace) {
      Object.assign(run, { ticker: loadedTrace.ticker || run.ticker, bucket: loadedTrace.bucket || null,
        date: loadedTrace.trade_date || null, policy: loadedTrace.policy_action || null,
        finalAction: loadedTrace.final_action || null });
    }

    /* Which desks this run seated, in the run's own words: its start event while it runs,
       its record once finished. The configured selection stands in only while a known
       instrument's own list has not arrived. Until anything names the instrument — no run
       yet, or a job the engine has not classified — no desk is marked as skipped. */
    const instrument = instrumentOf(progress && progress.instrument_type)
      || instrumentOf(loadedTrace && loadedTrace.instrument_type) || (run && run.instrument) || null;
    const configured = instrument ? ((team && team.analysts) || {})[instrument] || [] : null;
    const own = (progress || loadedTrace || {}).analysts || [];
    const analysts = own.length ? own : configured;

    const steps = (loadedTrace && loadedTrace.steps || []).filter((s) => s.role);
    const position = cursor === null ? steps.length - 1 : Math.min(cursor, steps.length - 1);

    const desks = roles.map((role) => {
      const seated = !role.analyst || !analysts || analysts.includes(role.analyst);
      const desk = { ...role, seated,
                     name: label('lab.role.' + role.key, role.key),
                     zone: label('lab.zone.' + (ZONE[role.group] || role.group), role.group) };
      if (!seated) return { ...desk, state: 'disabled', reason: skipReason(role, instrument) };
      if (progress) return { ...desk, ...fromProgress(progress, role.key, run, team) };
      if (loadedTrace) return { ...desk, ...fromTrace(steps, position, role.key) };
      return { ...desk, state: 'waiting' };
    });

    return { team, run, live, progress, trace, loadedTrace, steps, position,
             instrument, analysts, desks,
             mode: !run ? 'none' : run.live ? 'live'
                   : run.broken ? 'broken' : cursor === null ? 'done' : 'replay' };
  }

  function skipReason(role, instrument) {
    if (!instrument) return C.t('lab.notApplicable');
    const key = 'lab.skip.' + role.key + '.' + instrument;
    const generic = C.t('lab.skip.generic', { instrument: label('lab.' + instrument, instrument) });
    return label(key, generic);
  }

  /* Live desks. The service reports two states because two are all an event supports:
     a call is open, or the last one returned. Everything else below is this page's own
     judgement, made from the clock and the job state, and is labelled as such. */
  function fromProgress(progress, key, run, team) {
    const desk = (progress.desks || {})[key];
    if (!desk) return { state: run.broken ? 'waiting' : 'waiting' };
    const idle = secondsSince(desk.last_at);
    const timeout = (team && team.call_timeout_seconds) || 900;
    if (desk.state === 'working') {
      if (run.broken) return { state: 'error', elapsed: idle, calls: desk.calls };
      /* A call that has outlived the adapter's own timeout cannot still be running, so
         this is a stall in fact and not a guess about a slow model. */
      if (idle !== null && idle > timeout) return { state: 'stalled', elapsed: idle, calls: desk.calls };
      return { state: desk.tools.length ? 'research' : 'thinking', elapsed: idle,
               calls: desk.calls, tools: desk.tools };
    }
    return { state: 'done', elapsed: desk.wall_seconds, calls: desk.calls, tools: desk.tools };
  }

  /* A finished run, stepped through. Before the cursor a desk has reported; after it,
     the run had not reached that desk yet at the moment being replayed. */
  function fromTrace(steps, position, key) {
    const index = steps.findIndex((s) => s.role === key);
    if (index < 0) return { state: 'waiting' };
    const step = steps[index];
    if (index > position) return { state: 'waiting', step };
    return { state: 'done', step, chars: step.chars,
             summary: firstLine(step.markdown) };
  }

  /* The engine writes markdown; the desk shows its opening sentence verbatim. Nothing is
     translated or rephrased — this is the engine's own record of what it concluded. */
  function firstLine(markdown) {
    if (!markdown) return '';
    for (const block of String(markdown).split(/\n\s*\n/)) {
      let line = block.replace(/\s+/g, ' ').trim();
      if (!line || line.startsWith('#') || line.startsWith('|') || line.startsWith('---')) continue;
      /* The engine writes markdown and the desk shows one line of it as text. Only the
         markup characters are dropped; every word stays exactly as the engine wrote it. */
      line = line.replace(/^[-*+]\s+/, '').replace(/\*\*(.+?)\*\*/g, '$1')
                 .replace(/(^|\s)\*(\S.*?\S)\*/g, '$1$2').replace(/`([^`]+)`/g, '$1');
      return line.length > 240 ? line.slice(0, 240) + '…' : line;
    }
    return '';
  }

  /* ── painting ─────────────────────────────────────────────────────── */
  function render() {
    if (document.hidden) return;
    const v = view();
    if (!v.desks.length) return;
    if (!chosenRole || !v.desks.some((d) => d.key === chosenRole)) {
      chosenRole = (v.desks.find((d) => d.state === 'thinking' || d.state === 'research')
                    || v.desks.find((d) => d.seated) || v.desks[0]).key;
    }
    syncWatch(v.run);
    paintNow(v);
    paintRun(v);
    paintScene(v);
    paintSelected(v);
    paintRoster(v);
    paintGate(v);
    paintFeed(v);
    paintSubjects();
    startAmbience(v);
  }

  /* Point the 5-second run poll at the run being shown, and only while it is live. */
  function syncWatch(run) {
    const want = run && run.live ? run.key : null;
    if (CT_API.activeRun !== want) CT_API.watchRun(want);
  }

  function paintNow(v) {
    const schedule = D.schedule || {};
    const working = v.desks.find((d) => ['thinking', 'research'].includes(d.state));
    let title = '', caption = '';
    if (!v.run) {
      title = C.t('lab.noRuns');
      caption = schedule.next_run_at ? C.t('lab.nextAt', { at: C.dateTime(schedule.next_run_at) }) : '';
    } else if (working) {
      title = working.name + ' · ' + label('lab.state.' + working.state, working.state);
      caption = working.elapsed === null || working.elapsed === undefined
        ? C.t('lab.sinceUnknown') : C.t('lab.elapsed', { n: working.elapsed });
    } else if (v.run.live) {
      title = C.t('lab.runStarting', { ticker: v.run.ticker });
      caption = C.t('lab.runState', { state: C.code(v.run.state) });
    } else if (v.run.broken) {
      title = C.t('lab.runStopped', { ticker: v.run.ticker });
      caption = v.run.error || C.t('lab.runState', { state: C.code(v.run.state) });
    } else {
      /* The ticker heads the card, so the line under it is whose run, when, and how it ended. */
      title = [accountOf(v.run), v.run.date ? C.dateOnly(v.run.date) : '',
               C.code(v.run.finalAction || v.run.policy || 'COMPLETE')].filter(Boolean).join(' · ');
      caption = v.loadedTrace && v.loadedTrace.elapsed_seconds
        ? C.t('lab.runTook', { t: C.clock(v.loadedTrace.elapsed_seconds) }) : C.t('lab.runArchived');
    }
    q('#now-title').textContent = title;
    q('#now-caption').textContent = caption;

    const budget = q('.lab-budget');
    if (budget) {
      const used = schedule.analyses_today, total = schedule.daily_budget;
      budget.innerHTML = used === undefined || total === undefined
        ? '<strong>—</strong><small>' + C.escapeHtml(C.t('lab.daily')) + '</small>'
        : '<strong>' + C.escapeHtml(String(used)) + ' <span>/ ' + C.escapeHtml(String(total)) +
          '</span></strong><small>' + C.escapeHtml(C.t('lab.daily')) + '</small>';
    }
  }

  function paintRun(v) {
    q('#symbol').textContent = v.run ? v.run.ticker : '—';
    q('#instrument').hidden = !v.instrument;
    q('#instrument').textContent = v.instrument ? label('lab.' + v.instrument, v.instrument) : '';
    q('#office-title').textContent = C.t('lab.officeTitle', { symbol: v.run ? v.run.ticker : '—' });
    const badge = q('#mode-badge');
    badge.textContent = label('lab.mode.' + v.mode, v.mode);
    badge.dataset.mode = v.mode;
    /* A run in flight has no report yet; the office is where it is watched. */
    q('#lab-report').hidden = !v.run || v.run.live;
    /* Looking back at another run while one is working: one tap returns to the one working. */
    const working = runList().find((r) => r.live);
    q('#lab-back-live').hidden = !working || !v.run || v.run.key === working.key;
    /* A run going nowhere otherwise holds a concurrency slot and a share of the day's budget
       until its timeout; the server decides whether its state still allows a stop. */
    const cancel = q('#lab-cancel');
    cancel.hidden = !v.run || !v.run.live;
    if (v.run && v.run.live) {
      cancel.disabled = !!stopping;
      cancel.textContent = stopping && stopping.key === v.run.key ? C.t('rs.cancelling')
        : stopNote && stopNote.key === v.run.key ? stopNote.text : C.t('rs.cancel');
    }

    /* Stepping only means something for a run whose reports exist. */
    const replayable = !v.run || v.run.live ? false : v.steps.length > 0;
    q('#replay-controls').hidden = !replayable;
    if (replayable) {
      q('#event-number').textContent = C.t('lab.stepOf', {
        current: String(v.position + 1).padStart(2, '0'),
        total: String(v.steps.length).padStart(2, '0'),
      });
      q('#previous').disabled = v.position <= 0;
      q('#next').disabled = v.position >= v.steps.length - 1;
    }
    /* A settled run ends on when the next one is due. What is left of today's budget is
       already in the head, and with no run at all the caption above says it. */
    const note = q('#rest-note');
    const schedule = D.schedule || {};
    note.hidden = !(v.mode === 'done' || v.mode === 'replay') || !schedule.next_run_at;
    if (!note.hidden) note.textContent = C.t('lab.nextAt', { at: C.dateTime(schedule.next_run_at) });
    const demo = q('.lab-demo-note');
    if (demo) {
      demo.hidden = !window.CT_DEMO && !(v.trace && v.trace.failed);
      demo.textContent = window.CT_DEMO ? C.t('lab.demoNote')
        : v.trace && v.trace.failed ? C.t('lab.traceFailed', { err: v.trace.failed }) : '';
      demo.dataset.kind = window.CT_DEMO ? 'demo' : 'error';
    }
  }

  function paintScene(v) {
    const byKey = new Map(v.desks.map((d) => [d.key, d]));
    const seated = SEATS.map((key, i) => {
      const desk = byKey.get(key) || { key, name: key, state: 'disabled' };
      return { ...desk, id: key, ...window.labPixelScene.seats[i] };
    });
    q('#people').innerHTML = seated.map((d) =>
      '<button class="person" data-role="' + C.escapeHtml(d.id) + '" data-state="' + C.escapeHtml(d.state) +
        '" data-pose="seated" data-label="' + (d.y < 40 ? 'below' : 'above') +
        '" data-edge="' + (d.x < 25 ? 'start' : d.x > 75 ? 'end' : 'middle') +
        '" data-zone="' + C.escapeHtml(ZONE[d.group] || '') +
        '" aria-pressed="' + (chosenRole === d.id) + '" aria-label="' +
        C.escapeHtml(d.name + ', ' + label('lab.state.' + d.state, d.state)) + '" style="left:' + d.x +
        '%;top:' + d.y + '%;z-index:' + Math.round(d.y) + '">' +
        '<span class="desk-label">' + C.escapeHtml(d.name) + ' · ' +
          C.escapeHtml(label('lab.state.' + d.state, d.state)) +
          (['thinking', 'research'].includes(d.state)
            ? '<span class="thinking-dots" aria-hidden="true"><i></i><i></i><i></i></span>' : '') +
        '</span></button>').join('');
    q('#scene-caption').textContent = C.t('lab.sceneHelp');
    /* A walk is a reader-triggered walkthrough of a handoff. During a live run it would
       read as the engine moving someone, so it is only offered once a run has settled. */
    const walkable = v.run && !v.run.live;
    q('#walk').hidden = !walkable;
    window.labPixelScene.sync({ roles: seated, selected: chosenRole, active: canAnimate() });
  }

  function paintSelected(v) {
    const desk = v.desks.find((d) => d.key === chosenRole) || v.desks[0];
    const state = desk.state;
    const index = SEATS.indexOf(desk.key);
    /* The worker at this desk, cropped to head and shoulders by lab.css. The second six share
       the first six's sheets and are recoloured in the room, so they are here too. */
    const portrait = q('#portrait');
    portrait.style.backgroundImage = "url('assets/lab/pixel-agents/characters/char_" +
      (index < 0 ? 0 : index % 6) + ".png')";
    portrait.style.filter = index >= 6 ? 'hue-rotate(' + [25, -20, 55, -30, 30, 60][index - 6] + 'deg)' : '';

    q('#selected-name').textContent = desk.name;
    q('#selected-zone').textContent = desk.zone;
    q('#selected-state').textContent = label('lab.state.' + state, state);
    q('#selected-state').dataset.state = state;

    let meta = '', bubble = '';
    if (state === 'disabled') {
      meta = C.t('lab.notApplicable');
      bubble = desk.reason || '';
    } else if (state === 'done' && desk.step) {
      meta = C.t('lab.reportSize', { n: (Math.round(desk.chars / 100) / 10) + 'k' });
      bubble = desk.summary || C.t('lab.reportEmpty');
    } else if (state === 'done') {
      meta = desk.calls ? C.t('lab.deskCalls', { n: desk.calls, t: C.clock(desk.elapsed || 0) }) : '';
      bubble = desk.tools && desk.tools.length
        ? C.t('lab.deskTools', { tools: desk.tools.join(', ') }) : C.t('lab.deskHandedOff');
    } else if (state === 'thinking' || state === 'research') {
      meta = desk.elapsed === null || desk.elapsed === undefined
        ? C.t('lab.sinceUnknown') : C.t('lab.stateElapsed', { n: desk.elapsed });
      bubble = state === 'research' && desk.tools && desk.tools.length
        ? C.t('lab.deskTools', { tools: desk.tools.join(', ') }) : C.t('lab.bubbleThinking');
    } else if (state === 'stalled') {
      meta = C.t('lab.stalledFor', { n: Math.round((desk.elapsed || 0) / 60) });
      bubble = label('lab.description.stalled', state);
    } else if (state === 'error') {
      meta = v.run && v.run.error ? v.run.error : C.t('lab.runState', { state: C.code(v.run ? v.run.state : '') });
      bubble = label('lab.description.error', state);
    } else {
      meta = C.t('lab.notStarted');
      bubble = C.t('lab.bubbleWaiting');
    }
    q('#selected-meta').textContent = meta;
    q('#selected-bubble').textContent = bubble;
    q('#selected-reason').textContent = desk.key === 'portfolio' ? C.t('lab.portfolioLimit') : '';
  }

  function paintRoster(v) {
    const zones = [];
    v.desks.forEach((d) => { if (!zones.includes(d.zone)) zones.push(d.zone); });
    q('#team-count').textContent = v.analysts
      ? C.t('lab.enabledCount', { n: v.desks.filter((d) => d.seated).length }) : '';
    q('#roster').innerHTML = zones.map((zone) =>
      '<div class="roster-zone"><div class="zone-name">' + C.escapeHtml(zone) + '</div>' +
      v.desks.filter((d) => d.zone === zone).map((d) => {
        const line = d.state === 'disabled' ? d.reason
          : d.summary ? d.summary
          : d.tools && d.tools.length ? C.t('lab.deskTools', { tools: d.tools.join(', ') }) : '';
        return '<button class="role-button" data-role="' + C.escapeHtml(d.key) + '" data-state="' +
          C.escapeHtml(d.state) + '" aria-pressed="' + (d.key === chosenRole) + '">' +
          '<span>' + C.escapeHtml(d.name) +
            (line ? '<small class="role-summary">' + C.escapeHtml(line) + '</small>' : '') + '</span>' +
          '<span class="role-status">' + C.escapeHtml(label('lab.state.' + d.state, d.state)) + '</span>' +
          '</button>';
      }).join('') + '</div>').join('');
  }

  /* The gate is deliberately not a desk: no agent decides this, and no confident report
     can widen it. It shows only what the deterministic side actually returned. */
  function paintGate(v) {
    const result = v.loadedTrace || (v.progress && v.progress.result) || null;
    const settled = result && (result.policy_action || result.hard_risk_verdict || result.hard_risk);
    const policy = result && result.policy_action;
    const hard = result && (result.hard_risk_verdict || result.hard_risk);
    const confidence = result && result.confidence;
    q('#gate-result').innerHTML = settled
      ? '<div class="gate-grid">' +
          '<div><span>' + C.escapeHtml(C.t('lab.policy')) + '</span><strong>' +
            C.escapeHtml(C.code(policy || '—')) + '</strong></div>' +
          '<div><span>' + C.escapeHtml(C.t('lab.hardRisk')) + '</span><strong>' +
            C.escapeHtml(C.code(hard || '—')) + '</strong></div>' +
          (confidence === null || confidence === undefined ? '' :
            '<div><span>' + C.escapeHtml(C.t('lab.confidence')) + '</span><strong>' +
            C.escapeHtml(String(confidence)) + '</strong></div>') +
        '</div>' +
        (policy === 'NO_TRADE'
          ? '<p class="lab-caption">' + C.escapeHtml(C.t('lab.noTradeExplanation')) + '</p>' : '')
      : '<p>' + C.escapeHtml(C.t('lab.awaitResearch')) + '</p>' +
        '<p class="lab-caption">' + C.escapeHtml(C.t('lab.awaitDecision')) + '</p>';

    const button = q('.lab-approval');
    const waiting = v.run && v.run.proposalId;
    button.textContent = waiting ? C.t('lab.reviewProposal') : settled ? C.t('lab.noApproval') : C.t('lab.awaitRules');
    button.disabled = !waiting;
    button.onclick = waiting ? () => {
      const link = document.createElement('a');
      link.href = 'approvals.html';
      button.after(link); link.click(); link.remove();
    } : null;
  }

  function paintFeed(v) {
    let rows = [];
    if (v.progress) {
      /* Real timestamps, because the events carry them. */
      rows = (v.progress.events || []).slice().reverse().slice(0, 8).map((e) => ({
        time: clockTime(e.t), text: eventText(e, v),
      }));
    } else if (v.steps.length) {
      /* A finished run's reports carry an order, not a clock. Numbering them is the
         honest reading; a timestamp here would be invented. */
      rows = v.steps.slice(0, v.position + 1).reverse().map((s) => ({
        time: '#' + String(s.order).padStart(2, '0'),
        text: C.t('lab.feedStep', {
          role: label('lab.role.' + s.role, s.label),
          n: (Math.round(s.chars / 100) / 10) + 'k',
        }),
      }));
    }
    q('#events').innerHTML = rows.length
      ? rows.map((r) => '<li><time>' + C.escapeHtml(r.time) + '</time>' + C.escapeHtml(r.text) + '</li>').join('')
      : '<li class="lab-caption">' + C.escapeHtml(C.t('lab.feedEmpty')) + '</li>';
  }

  function eventText(event, v) {
    const name = event.role ? label('lab.role.' + event.role, event.role) : (event.node || '');
    if (event.kind === 'run_started') return C.t('lab.feedStarted', { ticker: event.ticker || (v.run && v.run.ticker) || '' });
    if (event.kind === 'run_finished') return C.t('lab.feedFinished', { action: C.code(event.policy_action || '—') });
    if (event.kind === 'call_started') return C.t('lab.feedCallStarted', { role: name });
    if (event.kind === 'call_finished') {
      return (event.tools || []).length
        ? C.t('lab.feedCallTools', { role: name, t: C.clock(event.wall_seconds || 0), tools: event.tools.join(', ') })
        : C.t('lab.feedCallDone', { role: name, t: C.clock(event.wall_seconds || 0) });
    }
    return name;
  }


  /* ── what the room studies: what the owner holds, then what they added ── */
  const SHOWN = 8;
  let starting = null, refreshing = false, refreshKey = null, refreshNote = '';
  /* What became of the last tap on "Research", said on the row it was tapped for. */
  let rowNote = null;
  /* A repeated tap after an answer that never arrived reuses its request id, so the server
     returns the run it already started instead of starting another (and spending twice). */
  const drafts = new Map();
  const holderName = (holder) => holder.scope === 'agentic' ? C.t('acct.micro_live')
    : C.t('lab.ownAccount', { label: holder.account_label || '' });

  function subjectRow(subject) {
    const h = C.escapeHtml, latest = subject.latest, running = subject.running;
    const where = subject.holders.length ? subject.holders.map(holderName).join(' · ')
      : subject.lists.length ? C.t('lab.onLists', { lists: subject.lists.join(', ') }) : '';
    let state, status, open = '';
    if (running) {
      state = 'running';
      status = C.t(running.state === 'QUEUED' ? 'lab.queued' : 'lab.researching');
      open = ' data-watch="' + h(running.run_key) + '"';
    } else if (latest) {
      state = 'done';
      status = [C.code(latest.final_action || latest.policy_action || 'COMPLETE'),
                latest.confidence == null ? '' : C.t('lab.confidenceShort', { n: latest.confidence })]
        .filter(Boolean).join(' · ');
      open = ' data-trace="' + h(latest.run_id) + '"';
    } else {
      state = 'none';
      status = '';
    }
    /* A name never studied says so first, where the eye lands; the button beside it studies it. */
    const sub = [state === 'none' ? C.t('lab.notResearched') : '', where,
      latest && !running ? C.dateOnly(latest.trade_date) + (latest.account_label &&
      !subject.holders.some((holder) => holder.account_label === latest.account_label)
      ? ' · ' + latest.account_label : '') : ''].filter(Boolean).join(' · ');
    const note = rowNote && rowNote.symbol === subject.symbol ? rowNote : null;
    const tag = open ? 'button' : 'div';
    return '<div class="lab-subject" data-state="' + state + '">' +
      '<' + tag + ' class="lab-subject-main"' + (open ? ' type="button"' + open : '') + '>' +
        '<span class="lab-subject-copy"><span class="lab-subject-line"><strong>' + h(subject.symbol) + '</strong>' +
          (status ? '<span class="lab-subject-state">' + h(status) + '</span>' : '') + '</span>' +
          (sub ? '<span class="lab-subject-sub">' + h(sub) + '</span>' : '') +
          (note ? '<span class="lab-subject-note" role="status" data-kind="' + note.kind + '">' + h(note.text) + '</span>' : '') +
        '</span>' +
        (open ? '<span class="lab-chevron" aria-hidden="true"></span>' : '') +
      '</' + tag + '>' +
      (state === 'none' ? '<button class="lab-pill lab-subject-start" type="button" data-start="' + h(subject.symbol) + '"' +
        (starting ? ' disabled' : '') + '>' + h(C.t(starting === subject.symbol ? 'rs.starting' : 'lab.research')) + '</button>' : '') +
      '</div>';
  }

  function subjectList(rows, which, empty) {
    if (!rows.length) return '<p class="lab-fine lab-subjects-empty">' + C.escapeHtml(empty) + '</p>';
    /* What is being studied now comes first; the rest keep the order the schedule reads. */
    const ordered = [...rows.filter((s) => s.running), ...rows.filter((s) => !s.running)];
    const shown = expanded[which] ? ordered : ordered.slice(0, SHOWN);
    return shown.map(subjectRow).join('') + (ordered.length > SHOWN
      ? '<button class="lab-more" type="button" data-more="' + which + '" aria-expanded="' + expanded[which] + '">' +
        C.escapeHtml(C.t(expanded[which] ? 'lab.showFewer' : 'lab.showAll', { n: ordered.length })) + '</button>' : '');
  }

  function paintSubjects() {
    const all = subjects();
    const failed = !window.CT_DEMO && !!CT_API.state?.resources?.researchSubjects?.error;
    const snapshot = (all && all.snapshot) || {};
    /* A refresh asked for here is finished once its job is; the lists are then read again. */
    const asked = refreshKey && (D.researchJobs || []).find((j) => j.run_key === refreshKey);
    if (asked && !RUNNING.includes(asked.state)) {
      refreshNote = asked.state === 'COMPLETE' ? '' : C.t('lab.readFailed', { err: asked.error || C.code(asked.state) });
      refreshKey = null;
      CT_API.readOnce?.('researchSubjects');
    }
    const held = (all && all.held) || [], added = (all && all.added) || [];
    q('#held-count').textContent = all ? String(held.length) : '';
    C.setHTML(q('#held-list'), !all ? '<p class="lab-fine">' + C.escapeHtml(C.t(failed ? 'lab.subjectsUnavailable' : 'common.loading')) + '</p>'
      : subjectList(held, 'held', C.t(snapshot.read ? 'lab.heldEmpty' : 'lab.heldUnread')));
    C.setHTML(q('#added-list'), !all ? ''
      : subjectList(added, 'added', C.t(snapshot.read ? 'lab.addedEmpty' : 'lab.addedUnread')));
    q('#subjects-note').textContent = [
      snapshot.read ? C.t('lab.readAt', { at: C.dateTime(snapshot.fetched_at) }) : '',
      ...((all && all.errors) || []),
      refreshing || refreshKey ? C.t('lab.reading') : refreshNote,
    ].filter(Boolean).join(' · ');
    const refresh = q('#subjects-refresh');
    refresh.disabled = refreshing || !!refreshKey;
  }

  async function startResearch(symbol) {
    const subject = subjectFor(symbol);
    if (!subject || starting) return;
    /* Asked from a report: the answer is said on the name's row, so the sheet steps aside. */
    report.close();
    if (window.CT_DEMO || !CT_API.writesEnabled) {
      rowNote = { symbol: subject.symbol, kind: 'info', text: C.t(window.CT_DEMO ? 'lab.demoStart' : 'lab.signInToStart') };
      render(); return;
    }
    const plan = subject.start || {};
    const personal = plan.scope === 'personal';
    const body = personal
      ? { kind: 'analysis', ticker: subject.symbol, account_id: plan.account_id, scope: 'personal',
          language: window.I18N?.lang === 'zh' ? 'zh' : 'en' }
      : { ticker: subject.symbol, bucket: plan.bucket, scope: 'agentic', purpose: plan.purpose,
          instrument_type: 'auto', trigger: 'manual' };
    const signature = JSON.stringify(body);
    let draft = drafts.get(subject.symbol);
    if (!draft || draft.signature !== signature) {
      draft = { signature, id: crypto.randomUUID() };
      drafts.set(subject.symbol, draft);
    }
    body.request_id = draft.id;
    starting = subject.symbol; rowNote = null; render();
    try {
      const res = await CT_API.post(personal ? '/advisor/jobs' : '/research', body);
      if (res.blocked) { rowNote = { symbol: subject.symbol, kind: 'info', text: C.t('lab.signInToStart') }; return; }
      drafts.delete(subject.symbol);
      /* The office turns to the new run at once; its desks light as its events arrive. */
      stopMotion();
      chosenRun = res.run_key; cursor = null; chosenRole = null;
      ['researchJobs', 'researchSubjects', 'schedule', 'research'].forEach((key) => CT_API.readOnce(key));
      q('.lab-office').scrollIntoView({ block: 'start', behavior: reduced.matches ? 'auto' : 'smooth' });
    } catch (error) {
      rowNote = { symbol: subject.symbol, kind: 'error', text: C.t('lab.startFailed', { ticker: subject.symbol, err: error.message }) };
    } finally {
      starting = null; render();
    }
  }

  let stopping = null, stopNote = null;
  async function stopRun() {
    const run = selectedRun();
    if (!run || !run.live || stopping) return;
    if (window.CT_DEMO || !CT_API.writesEnabled) {
      stopNote = { key: run.key, text: C.t(window.CT_DEMO ? 'lab.demoStart' : 'lab.signInToStart') }; render(); return;
    }
    stopping = { key: run.key }; stopNote = null; render();
    try {
      const result = await CT_API.post('/research/' + encodeURIComponent(run.key) + '/cancel', {});
      if (result.blocked) stopNote = { key: run.key, text: C.t('lab.signInToStart') };
      ['researchRun', 'researchJobs', 'researchSubjects'].forEach((key) => CT_API.readOnce(key));
    } catch (error) {
      stopNote = { key: run.key, text: C.t('rs.cancelFailed', { err: error.message }) };
    } finally {
      stopping = null; render();
    }
  }

  let refreshRequest = null;
  async function readRobinhood() {
    if (refreshing || refreshKey) return;
    if (window.CT_DEMO || !CT_API.writesEnabled) {
      refreshNote = C.t(window.CT_DEMO ? 'lab.demoStart' : 'lab.signInToStart'); render(); return;
    }
    refreshing = true; refreshNote = ''; render();
    refreshRequest ||= crypto.randomUUID();
    try {
      const job = await CT_API.post('/advisor/jobs', { kind: 'refresh', scope: 'personal', request_id: refreshRequest,
        language: window.I18N?.lang === 'zh' ? 'zh' : 'en' });
      if (job.blocked) { refreshNote = C.t('lab.signInToStart'); return; }
      refreshRequest = null; refreshKey = job.run_key;
      CT_API.readOnce('researchJobs');
    } catch (error) {
      refreshNote = C.t('lab.readFailed', { err: error.message });
    } finally {
      refreshing = false; render();
    }
  }

  /* ── motion ───────────────────────────────────────────────────────── */
  function startAmbience(v) {
    document.documentElement.dataset.ambient = canAnimate() ? 'on' : 'off';
    /* An icon over the office: what it would do is its name, and the system's own
       reduced-motion setting leaves it nothing to do. */
    const button = q('#ambient');
    const name = C.t(reduced.matches ? 'lab.motionReduced'
      : ambientEnabled ? 'lab.motionPause' : 'lab.motionPlay');
    button.setAttribute('aria-label', name);
    button.title = name;
    button.setAttribute('aria-pressed', String(ambientEnabled && !reduced.matches));
    button.disabled = reduced.matches;
  }
  function stopMotion() {
    epoch += 1;
    clearTimeout(fileTimer);
    walking.clear();
    window.labPixelScene.stop();
    q('#flying-file').hidden = true;
    q('#walker').hidden = true;
    q('#walk').setAttribute('aria-pressed', 'false');
    q('#walk').textContent = C.t('lab.walk');
    document.documentElement.dataset.ambient = 'off';
  }

  /* One discrete handoff, drawn as the report moving. The people never move on their
     own: what travels between desks is a document, which is also what the graph passes. */
  function flyFile(fromKey) {
    if (reduced.matches || !canAnimate()) return;
    const toKey = HANDOFF[fromKey];
    if (!toKey) return;                       // the rating leaves the room, not another desk
    const from = window.labPixelScene.seats[SEATS.indexOf(fromKey)];
    const to = window.labPixelScene.seats[SEATS.indexOf(toKey)];
    if (!from || !to) return;
    const file = q('#flying-file'), mine = ++epoch;
    file.style.left = from.x + '%'; file.style.top = from.y + '%'; file.hidden = false;
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (mine !== epoch) return;
      file.style.left = to.x + '%'; file.style.top = to.y + '%';
    }));
    fileTimer = setTimeout(() => { file.hidden = true; }, 1350);
  }

  function startWalk() {
    const v = view();
    const desk = v.desks.find((d) => d.key === chosenRole);
    if (!desk) return;
    if (!desk.seated) { q('#scene-caption').textContent = C.t('lab.walkDisabled', { role: desk.name }); return; }
    const targetKey = HANDOFF[desk.key] || 'manager';
    const target = v.desks.find((d) => d.key === targetKey);
    if (!canAnimate()) {
      q('#scene-caption').textContent = C.t('lab.walkReduced', { role: desk.name, target: target.name });
      return;
    }
    const result = window.labPixelScene.walk(desk.key, targetKey, {
      message: C.t('lab.walkMessage'),
      queued: () => { q('#scene-caption').textContent = C.t('lab.walkQueued', { role: desk.name }); },
      start: () => {
        q('#walk').textContent = C.t('lab.walkActive');
        q('#scene-caption').textContent = C.t('lab.walkStarted', { role: desk.name, target: target.name });
      },
      done: () => {
        walking.delete(desk.key);
        q('#walk').textContent = C.t(walking.size ? 'lab.walkQueueActive' : 'lab.walk');
        q('#walk').setAttribute('aria-pressed', String(walking.size > 0));
        q('#scene-caption').textContent = C.t('lab.walkDone', { role: desk.name });
        render();
      },
    });
    if (result === 'duplicate') { q('#scene-caption').textContent = C.t('lab.walkDuplicate', { role: desk.name }); return; }
    if (result === 'reduced') return;
    walking.add(desk.key);
    q('#walk').textContent = C.t(result === 'queued' ? 'lab.walkAdded' : 'lab.walkActive');
    q('#walk').setAttribute('aria-pressed', 'true');
  }

  /* ── input ────────────────────────────────────────────────────────── */
  document.addEventListener('click', (event) => {
    const desk = event.target.closest && event.target.closest('[data-role]');
    if (!desk) return;
    chosenRole = desk.dataset.role;
    render();
    /* A desk picked from the list is read in the card above it, out of sight on a phone,
       so the card comes to the reader. The office sits right over the card and needs none. */
    if (desk.classList && desk.classList.contains('role-button')) {
      q('#desk-card').scrollIntoView({ block: 'nearest', behavior: reduced.matches ? 'auto' : 'smooth' });
    }
  });
  /* A name: its report, the office running it, or a first study of it. */
  function showRun(key) {
    stopMotion();
    chosenRun = key; cursor = null; chosenRole = null;
    render();
    q('.lab-office').scrollIntoView({ block: 'start', behavior: reduced.matches ? 'auto' : 'smooth' });
  }
  document.addEventListener('click', (event) => {
    const target = event.target.closest && event.target.closest('[data-trace], [data-watch], [data-replay], [data-start], [data-more]');
    if (!target) return;
    const d = target.dataset;
    if (d.trace) report.open(d.trace);
    else if (d.watch) showRun(d.watch);
    else if (d.replay) { report.close(); showRun(d.replay); }
    else if (d.start) startResearch(d.start);
    else if (d.more) { expanded[d.more] = !expanded[d.more]; render(); }
  });
  q('#lab-report').addEventListener('click', () => { const run = selectedRun(); if (run) report.open(run.key); });
  q('#lab-back-live').addEventListener('click', () => {
    const working = runList().find((r) => r.live);
    if (working) showRun(working.key);
  });
  q('#added-how').addEventListener('click', () => {
    const help = q('#added-help');
    help.hidden = !help.hidden;
    q('#added-how').setAttribute('aria-expanded', String(!help.hidden));
  });
  q('#subjects-refresh').addEventListener('click', readRobinhood);
  q('#lab-cancel').addEventListener('click', stopRun);
  q('#previous').addEventListener('click', () => step(-1));
  q('#next').addEventListener('click', () => step(1));
  function step(delta) {
    const v = view();
    if (!v.steps.length) return;
    stopMotion();
    cursor = Math.max(0, Math.min(v.steps.length - 1, v.position + delta));
    const current = v.steps[cursor];
    if (current) chosenRole = current.role;
    render();
    if (delta > 0 && current) flyFile(current.role);
  }
  q('#walk').addEventListener('click', startWalk);
  q('#ambient').addEventListener('click', () => { ambientEnabled = !ambientEnabled; stopMotion(); render(); });
  reduced.addEventListener('change', () => { stopMotion(); render(); });
  document.addEventListener('visibilitychange', () => { stopMotion(); render(); });
  document.addEventListener('ct:leave', stopMotion);
  document.addEventListener('ct:lang', render);

  /* A link names a run: a study still running is watched in the office; a finished one is
     drawn there and its report opens over it. Research links written before the Lab held
     the reports (research.html?run=) arrive here too. */
  let routed = null;
  function routeEntry() {
    const named = new URLSearchParams(window.location.search).get('run');
    if (!named || window.location.href === routed) return false;
    routed = window.location.href;
    chosenRun = named; cursor = null; chosenRole = null;
    if (!graphJobs().some((job) => job.run_key === named && RUNNING.includes(job.state))) report.open(named);
    return true;
  }
  document.addEventListener('ct:route', () => { if (routeEntry()) { stopMotion(); render(); } });
  document.addEventListener('ct:leave', () => { routed = null; });
  routeEntry();

  C.live(['research', 'researchJobs', 'schedule', 'labRoster'], render, ['researchRun', 'researchSubjects']);
});
