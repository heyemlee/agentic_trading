/* Same-origin API with authenticated writes and independent freshness per resource. */
window.CT_API = (function () {
  'use strict';
  const BASE = '';
  const demo = window.CT_DEMO === true;
  const TIMEOUT_MS = 15000;
  let activeRun = null;
  let pickedProposal = null;
  let linkedProposal = null;
  let evaluationScope = demo ? 'demo' : 'agentic';
  let assistantFilter = { scope: 'personal', account: '' };
  const assistantPath = () => '/advisor?scope=' + assistantFilter.scope +
    (assistantFilter.account ? '&account_id=' + encodeURIComponent(assistantFilter.account) : '');
  function matchesAssistant(data) {
    const f = assistantFilter;
    return data?.scope === f.scope && data.account_scope_kind === (f.scope === 'agentic' || f.account ? 'single' : 'all_personal') &&
      (data.account_id || '') === (f.scope === 'agentic' ? 'micro_live' : f.account);
  }
  let wanted = [];
  let auth = { authenticated: false, csrf_token: null };
  /* The polling cadence is FRONTEND_HANDOFF.md §15, and these numbers are a budget, not a
     preference. /micro-live reads the Robinhood venue on every call: two venue calls on the
     session the API keeps open for reads, and a full OAuth/MCP handshake of four to eight
     seconds whenever that session has to be opened again (broker/read_session.py). Until
     2026-09-16 every call opened a session of its own, and /orders did too until its
     venue-side list was dropped as a copy of what the Robinhood app shows; while a handshake
     runs it delays every other read, including ones that never touch a venue. On 2026-09-15
     this table was quickened to 5-15s across the board; the console then spent nearly all of
     its time handshaking, and a tab change sat on a skeleton for seconds waiting for a SQLite
     read that takes 26 ms on its own. Quickening a read here is therefore a decision about the
     whole console, not about one screen.

     `ms` is only how often a screen asks. How old a source's data may be before the console
     calls it out of date, and will not act on it, is `staleAfter`: three polls unless a source
     says otherwise. It used to be two polls, measured from when the last poll came back, so one
     slow answer on the five-second assistant poll put "expired" on the screen. Where the API
     stamps a reading with checked_at, age is measured from that instead, because the API keeps
     /portfolio and /micro-live between polls (service/kept.py): portfolio is renewed every four
     minutes and served for at most eight, micro-live every minute and served for at most one
     and a half. Their thresholds are that plus a poll, with room for one poll that fails. */
  const SOURCES = {
    // Chat progress wants the short poll; the reading itself is a local SQLite read.
    advisor: { path: () => '/advisor', ms: 5000, staleAfter: 300000 },
    assistant: { path: assistantPath, ms: 5000, staleAfter: 300000 },
    health: { path: () => '/health', ms: 30000 },
    schedule: { path: () => '/system/schedule', ms: 60000 },
    operations: { path: () => '/system/operations', ms: 60000 },
    notifications: { path: () => '/system/notifications', ms: 120000 },
    pushDevices: { path: () => '/notifications/devices', ms: 120000 },
    authSessions: { path: () => '/auth/sessions', ms: 60000 },
    config: { path: () => '/config', ms: 60000 },
    authority: { path: () => '/config/authority', ms: 30000 },
    attention: { path: () => '/attention', ms: 30000 },
    accounts: { path: () => '/accounts', ms: 60000 },
    evaluation: { path: () => '/evaluation?scope=' + evaluationScope, ms: 300000 },
    portfolio: { path: () => '/portfolio', ms: 60000, staleAfter: 600000 },
    risk: { path: () => '/risk', ms: 60000 },
    microLive: { path: () => '/micro-live', ms: 60000, staleAfter: 180000 },
    proposals: { path: () => '/proposals', ms: 15000 },
    orders: { path: () => '/orders', ms: 10000, staleAfter: 90000 },
    research: { path: () => '/research?limit=20', ms: 60000 },
    // What the Lab studies: the owner's holdings and watchlist names, each with its newest record.
    researchSubjects: { path: () => '/research/subjects', ms: 60000 },
    // The team and the analyst selection come from config/ and change about never,
    // so this is read once a session rather than polled like live state.
    labRoster: { path: () => '/research/roster', ms: 900000 },
    researchJobs: { path: () => '/research/jobs?limit=20', ms: 10000 },
    researchRun: { path: () => activeRun ? '/research/' + encodeURIComponent(activeRun) : null, ms: 5000 },
    proposalDetail: {
      ms: 15000,
      path: () => {
        if (linkedProposal) return '/proposals/' + encodeURIComponent(linkedProposal);
        /* The reader's pick wins for as long as it is still open. Once it
           closes — approved, declined, expired — it drops off this list and
           the queue falls back to the first, so the page is never blank while
           something is still waiting. */
        const list = window.CT.proposals || [];
        const chosen = list.find((p) => p.proposal_id === pickedProposal) || list[0];
        return chosen ? '/proposals/' + encodeURIComponent(chosen.proposal_id) : null;
      },
    },
  };
  const state = { online: null, demo, authPending: !demo, authChecked: false, authError: null,
    resources: {}, live: {}, lastOkAt: null, lastError: null };
  const timers = [];
  const inFlight = new Map();
  const opened = new Set(), opening = new Map();
  let sessionRequest = null, authEpoch = 0;

  const staleAfter = src => src.staleAfter || Math.max(src.ms * 3, 30000);
  /* Live while the data is younger than its source allows: aged from when the API read it
     (dataAt) where the API says, from when it arrived otherwise. */
  function isLive(key) {
    if (demo) return false;
    if (!key) return wanted.every(k => !SOURCES[k].path() || isLive(k));
    const item = state.resources[key];
    if (!item || item.error || !item.lastOkAt) return false;
    return Date.now() - (item.dataAt || item.lastOkAt) <= staleAfter(SOURCES[key]);
  }
  /* How far this device's clock runs ahead of the API's, from the Date header of the latest
     answer. A time the API stamped is moved onto this clock before its age is taken; a phone a
     few minutes fast would otherwise see every reading as that much older than it is. */
  let clockSkew = 0;
  function noteServerClock(res) {
    const stamp = Date.parse(res.headers && typeof res.headers.get === 'function' ? res.headers.get('date') || '' : '');
    if (Number.isFinite(stamp)) clockSkew = Date.now() - stamp;
  }
  /* checked_at as this device's clock would have read it, never later than its arrival.
     Python writes microseconds, which not every browser's Date.parse accepts: cut to three. */
  function dataTime(data, arrived) {
    if (!data || Array.isArray(data) || typeof data.checked_at !== 'string') return arrived;
    const stamp = Date.parse(data.checked_at.replace(/(\.\d{3})\d+/, '$1'));
    return Number.isFinite(stamp) ? Math.min(stamp + clockSkew, arrived) : arrived;
  }
  let connectionSignature = '';
  function announce(changed = false) {
    state.online = !demo && wanted.length > 0 && isLive();
    Object.keys(SOURCES).forEach(key => { state.live[key] = isLive(key); });
    document.dispatchEvent(new CustomEvent('ct:connection', { detail: state }));
    const signature = JSON.stringify([state.authPending, state.authChecked, state.authError, auth.authenticated, state.live,
      Object.entries(state.resources).map(([key, item]) => [key, item.error])]);
    if (changed || signature !== connectionSignature) {
      connectionSignature = signature;
      document.dispatchEvent(new CustomEvent('ct:data', { detail: state }));
    }
  }
  function unauthorized(clearDrafts = true) {
    authEpoch++;
    opened.clear(); opening.clear();
    window.CT.researchSubjects = null; window.CT.accounts = null;
    window.CT.assistant = null;
    state.authChecked = true; state.authPending = false; state.authError = null;
    if (clearDrafts) { try { window.sessionStorage.removeItem('ct.advisor.session.v1'); } catch (_) {} }
    auth = { authenticated: false, csrf_token: null };
    Object.keys(state.resources).forEach(key => { state.resources[key].error = 'login required'; });
    document.dispatchEvent(new CustomEvent('ct:auth', { detail: { ...auth, clearDrafts } }));
  }
  async function request(path, options = {}, timeout = TIMEOUT_MS) {
    if (demo) throw new Error('Demo mode has no API access.');
    if (!path.startsWith('/') || path.startsWith('//')) throw new Error('API path must be same-origin.');
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(BASE + path, { ...options, credentials: 'same-origin', cache: 'no-store',
        signal: ctrl.signal, headers: { Accept: 'application/json', ...(options.headers || {}) } });
      noteServerClock(res);
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (res.status === 401) unauthorized();
        throw Object.assign(new Error(typeof body.detail === 'string' ? body.detail : res.status + ' ' + res.statusText),
          { status: res.status });
      }
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('Request timed out. Check the order state before retrying.');
      throw error;
    } finally { clearTimeout(timer); }
  }
  const fetchJSON = path => request(path);

  /* What a page shows from a read, for telling whether the read changed anything. A kept
     reading stamps checked_at every time it is read, so /health and /attention differed from
     the last answer on every poll and every page was drawn again for nothing. Freshness is still
     judged from checked_at (dataTime); only Evaluation prints it, so it keeps it here. */
  const shown = (key, data) => JSON.stringify(data, key === 'evaluation' ? undefined
    : (name, value) => (name === 'checked_at' ? undefined : value));

  async function readOnce(key) {
    const src = SOURCES[key];
    if (demo || !src) return;
    if (inFlight.has(key)) return inFlight.get(key);
    const path = src.path();
    if (!path) {
      if (key === 'proposalDetail') window.CT.proposalDetail = null;
      state.resources[key] = { lastOkAt: Date.now(), error: null, absent: true };
      announce();
      return;
    }
    const pending = (async () => {
      const epoch = authEpoch;
      const previous = shown(key, window.CT[key]);
      try {
        const data = await fetchJSON(path);
        // Never attach a late detail response to a newly selected proposal/run.
        if (epoch !== authEpoch || src.path() !== path) return;
        if (key === 'evaluation' && data.scope !== evaluationScope) {
          throw new Error(window.I18N.t('evl.scopeMismatch'));
        }
        if (key === 'assistant' && !matchesAssistant(data)) throw new Error(window.I18N.t('ai.scopeMismatch'));
        if (key === 'researchSubjects' && !(Array.isArray(data?.held) && Array.isArray(data?.added))) {
          throw new Error(window.I18N.t('rs.recordsInvalid'));
        }
        if (key === 'proposalDetail') {
          const current = (window.CT.proposals || []).find(p => p.proposal_id === data.proposal_id);
          if (linkedProposal ? data.proposal_id !== linkedProposal || (current && data.version !== current.version)
            : !current || data.proposal_id !== current.proposal_id || data.version !== current.version) {
            throw new Error('Proposal changed while its detail was loading. Refreshing the queue.');
          }
        }
        const payloadError = data && !Array.isArray(data) &&
          (data.error || data.errors && Object.keys(data.errors).length && JSON.stringify(data.errors));
        if (payloadError) {
          /* One payload carries its own failure and is still worth reading. A ledger
             that has gone unconfirmed answers with its buckets emptied and the reason
             in errors, and the page is written for exactly that shape — a gap and the
             reason, never a zero. Throwing it away instead leaves the reader on a blank
             screen that cannot say why or offer the way out. It stays marked incomplete,
             so canAct() still refuses and nothing may be approved against it. */
          const partial = key === 'portfolio' && !!data.buckets && !!data.accounts || key === 'accounts' && Array.isArray(data.accounts);
          if (partial) {
            window.CT[key] = data;
            state.resources[key] = { ...(state.resources[key] || {}), receivedAt: Date.now(), error: String(payloadError), path };
            return;
          }
          throw new Error(String(payloadError));
        }
        window.CT[key] = data;
        const arrived = Date.now();
        state.resources[key] = { lastOkAt: arrived, dataAt: dataTime(data, arrived), error: null, path };
        state.lastOkAt = arrived;
        if (key === 'proposals') {
          const current = linkedProposal ? data.find(p => p.proposal_id === linkedProposal)
            : data.find(p => p.proposal_id === pickedProposal) || data[0];
          const detail = window.CT.proposalDetail;
          const changed = detail && current && (detail.proposal_id !== current.proposal_id || detail.version !== current.version);
          const removed = !current && (!linkedProposal || detail?.status === 'WAITING_FOR_APPROVAL');
          if (changed || removed) {
            window.CT.proposalDetail = null;
            state.resources.proposalDetail = { lastOkAt: null, error: 'proposal detail needs refresh' };
          }
          if (wanted.includes('proposalDetail')) Promise.resolve().then(() => readOnce('proposalDetail'));
        }
      } catch (error) {
        if (epoch !== authEpoch || src.path() !== path) return;
        state.resources[key] = { ...(state.resources[key] || {}), error: error.message };
        state.lastError = error.message;
      } finally { announce(previous !== shown(key, window.CT[key])); }
    })();
    inFlight.set(key, pending);
    try { return await pending; }
    finally {
      inFlight.delete(key);
      if (src.path() !== path) readOnce(key);
    }
  }
  function stop() { while (timers.length) clearInterval(timers.pop()); }
  async function loadSession() {
    if (demo) return;
    if (sessionRequest) return sessionRequest;
    const epoch = authEpoch;
    state.authPending = true;
    state.authError = null;
    announce();
    sessionRequest = (async () => {
      try {
        const session = await fetchJSON('/auth/session');
        // A concurrent 401 or logout takes precedence over this older request.
        if (epoch !== authEpoch) return;
        auth = session;
        state.authChecked = true;
        if (!auth.authenticated) unauthorized();
      } catch (error) {
        if (epoch !== authEpoch) return;
        auth = { authenticated: false, csrf_token: null };
        state.authChecked = false;
        state.authError = error.message;
      } finally {
        state.authPending = false;
        document.dispatchEvent(new CustomEvent('ct:auth', { detail: auth }));
        announce();
      }
    })();
    try { await sessionRequest; } finally { sessionRequest = null; }
  }
  function start(keys) {
    stop();
    wanted = [...new Set((keys || []).filter(key => SOURCES[key]))];
    wanted.forEach(key => { state.resources[key] ||= { lastOkAt: null, error: 'loading' }; });
    if (wanted.includes('proposalDetail') && !wanted.includes('proposals')) wanted.push('proposals');
    if (demo) { announce(); return; }
    if (state.authChecked && !auth.authenticated) { announce(true); return; }
    if (!state.authChecked) loadSession();
    // All reads already carry the HttpOnly cookie and are authorized by the server.
    // Waiting for the CSRF/session response only adds latency to a page navigation.
    wanted.forEach(key => { if (key !== 'proposalDetail' && !isLive(key)) readOnce(key); });
    announce(true);
    wanted.forEach(key => timers.push(setInterval(() => {
      if (!document.hidden) {
        if (state.authError) loadSession();
        readOnce(key);
      }
    }, SOURCES[key].ms)));
    timers.push(setInterval(() => { if (!document.hidden) announce(); }, 1000));
  }
  async function reconnect() {
    if (document.hidden || demo) return;
    await Promise.all([loadSession(), ...wanted.map(readOnce)]);
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) reconnect();
  });
  window.addEventListener?.('online', reconnect);
  window.addEventListener?.('pageshow', event => { if (event.persisted) reconnect(); });
  window.addEventListener?.('offline', () => {
    wanted.forEach(key => { state.resources[key] = { ...state.resources[key], error: 'network offline' }; });
    announce();
  });
  async function post(path, body) {
    if (demo) return { blocked: true, reason: 'Demo mode cannot change state.' };
    if (state.authPending || !auth.authenticated || !auth.csrf_token) {
      throw Object.assign(new Error('Sign in before performing this action.'), { notSent: true });
    }
    let result;
    try {
      result = await request(path, { method: 'POST', headers: {
        'Content-Type': 'application/json', 'X-CT-Request': '1', 'X-CSRF-Token': auth.csrf_token,
      }, body: JSON.stringify(body || {}) }, 60000);
    } catch (error) {
      /* 401 and 403 on a write come only from the API's security layer, before any route runs,
         so the action was not sent. Signing in from another tab replaces the session cookie and
         leaves this tab's CSRF token stale: every write answered 403 until the tab was hidden
         and shown again. The session is re-read now, so the next attempt carries the right
         token. Nothing is retried here; an approval is only ever sent by a person. */
      if (error.status === 401 || error.status === 403) {
        if (error.status === 403) loadSession();
        error.notSent = true;
      }
      throw error;
    }
    if (path === '/auth/logout') { unauthorized(); announce(); }
    return result;
  }
  /* The demo has one assistant fixture, CT.advisor, and narrows it the way the service narrows
     /advisor (service/assistant.py, advisor_state): one account keeps its own holdings, jobs and
     records plus the all-accounts ones that covered it, and Agentic carries no personal snapshot. */
  function demoAssistant(next) {
    const scoped = { scope: next.scope, account_scope_kind: next.scope === 'agentic' || next.account ? 'single' : 'all_personal',
      account_id: next.scope === 'agentic' ? 'micro_live' : next.account || null, account_label: next.scope === 'agentic' ? 'Agentic' : '' };
    const base = next.scope === 'personal' && window.CT.advisor;
    if (!base) return { ...scoped, jobs: [], decisions: [], conversation_summaries: [], latest_review: null };
    const covers = task => !next.account || task?.account_id === next.account ||
      task?.account_scope_kind === 'all_personal' && (task.account_ids || []).includes(next.account);
    const jobs = (base.jobs || []).filter(job => covers(job.task));
    const review = base.latest_review;
    return { ...base, ...scoped, jobs,
      snapshot: { ...base.snapshot, accounts: (base.snapshot?.accounts || []).filter(account => !next.account || account.id === next.account) },
      decisions: (base.decisions || []).filter(d => !next.account || d.account_id === next.account || d.account_scope_kind === 'all_personal'),
      latest_review: review && (!next.account || jobs.some(job => job.run_key === review.run_key)) ? review : null };
  }
  // Clear the previous account before starting a scoped read. Late responses are rejected above.
  function selectAssistant(filter) {
    if (!['personal', 'agentic'].includes(filter.scope)) return false;
    const next = { scope: filter.scope, account: filter.scope === 'personal' ? String(filter.account || '') : '' };
    if (JSON.stringify(next) === JSON.stringify(assistantFilter) && state.resources.assistant) return true;
    assistantFilter = next;
    window.CT.assistant = demo ? demoAssistant(next) : null;
    state.resources.assistant = { lastOkAt: null, error: null };
    announce(true);
    if (!demo) readOnce('assistant');
    return true;
  }
  // Clear the previous account before starting a scoped read. Late responses are rejected above.
  function selectEvaluation(scope) {
    if (!['demo', 'agentic', 'route_quality'].includes(scope)) return false;
    if (evaluationScope === scope && window.CT.evaluation?.scope === scope) return true;
    evaluationScope = scope;
    window.CT.evaluation = demo ? window.CT.evaluationExamples?.[scope] || null : null;
    state.resources.evaluation = { lastOkAt: null, error: null };
    announce(true);
    if (!demo) readOnce('evaluation');
    return true;
  }
  // Called by result views after display, never by readOnce or a polling callback.
  // Deduplication is in memory for this authenticated session; failed writes are not queued.
  async function markOpened(runKey) {
    if (demo || document.hidden || !auth.authenticated || state.authPending) return { blocked: true };
    if (!/^[A-Za-z0-9._-]+$/.test(runKey || '')) throw new Error('Invalid research run.');
    if (opened.has(runKey)) return { opened: true };
    if (opening.has(runKey)) return opening.get(runKey);
    const epoch = authEpoch;
    const request = (async () => {
      const result = await post('/research/' + encodeURIComponent(runKey) + '/opened', {});
      if (result.run_key !== runKey || !result.opened_at) throw new Error(window.I18N.t('rs.openedNotSaved'));
      if (epoch === authEpoch) {
        opened.add(runKey);
        await readOnce('attention');
      }
      return result;
    })();
    opening.set(runKey, request);
    try { return await request; }
    finally { if (opening.get(runKey) === request) opening.delete(runKey); }
  }
  /* Which of several waiting proposals is on screen. Mirrors watchRun: the
     source path reads it, so changing it re-points the same polled resource. */
  function selectProposal(proposalId, { includeClosed = false } = {}) {
    const link = includeClosed ? proposalId : null;
    if (proposalId === pickedProposal && linkedProposal === link) return pickedProposal;
    linkedProposal = link;
    pickedProposal = proposalId || null;
    /* Clearing the detail first avoids showing the old proposal's terms under
       the new one's heading while the read is in flight. Demo has no read to
       make, so there would be nothing to put back — leave it alone there. */
    if (!demo) {
      window.CT.proposalDetail = null;
      readOnce('proposalDetail');
    }
    return pickedProposal;
  }

  function watchRun(runKey) {
    activeRun = runKey || null;
    window.CT.researchRun = null;
    if (activeRun) readOnce('researchRun');
    return activeRun;
  }
  return { BASE, SOURCES, state, watchRun, selectProposal, selectEvaluation, selectAssistant, matchesAssistant, fetchJSON, readOnce, start, stop, post, markOpened, isLive, refresh: reconnect,
    get evaluationScope() { return evaluationScope; },
    get activeRun() { return activeRun; },
    get pickedProposal() { return pickedProposal; },
    get writesEnabled() { return !demo && !state.authPending && auth.authenticated === true; },
    get authenticated() { return auth.authenticated === true; },
    get username() { return auth.authenticated ? auth.username : null; },
    loaded: keys => demo || keys.every(key => !SOURCES[key].path() || !!(state.resources[key]?.lastOkAt || state.resources[key]?.receivedAt)),
    canAct: keys => !demo && !state.authPending && auth.authenticated === true && keys.every(isLive),
  };
})();
