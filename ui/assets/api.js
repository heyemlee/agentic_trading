/* Same-origin API with authenticated writes and independent freshness per resource. */
window.CT_API = (function () {
  'use strict';
  const BASE = '';
  const demo = window.CT_DEMO === true;
  const TIMEOUT_MS = 15000;
  let activeRun = null;
  let pickedProposal = null;
  let wanted = [];
  let auth = { authenticated: false, csrf_token: null };
  /* The polling cadence is FRONTEND_HANDOFF.md §15, and these numbers are a budget, not a
     preference. One of these reads opens a fresh Robinhood OAuth/MCP session at the API and
     holds it for four to eight seconds — /micro-live, on every call; /orders did too until
     its venue-side list was dropped as a copy of what the Robinhood app shows — and while
     that handshake runs it delays every other read, including ones that never touch a
     venue. On 2026-09-15 this table was quickened to
     5-15s across the board; the console then spent nearly all of its time handshaking, and a
     tab change sat on a skeleton for seconds waiting for a SQLite read that takes 26 ms on
     its own. Quickening a read here is therefore a decision about the whole console, not
     about one screen. */
  const SOURCES = {
    advisor: { path: () => '/advisor', ms: 5000 },
    health: { path: () => '/health', ms: 30000 },
    schedule: { path: () => '/system/schedule', ms: 60000 },
    operations: { path: () => '/system/operations', ms: 60000 },
    notifications: { path: () => '/system/notifications', ms: 120000 },
    pushDevices: { path: () => '/notifications/devices', ms: 120000 },
    authSessions: { path: () => '/auth/sessions', ms: 60000 },
    evaluation: { path: () => '/evaluation', ms: 300000 },
    portfolio: { path: () => '/portfolio', ms: 60000 },
    risk: { path: () => '/risk', ms: 60000 },
    microLive: { path: () => '/micro-live', ms: 60000 },
    proposals: { path: () => '/proposals', ms: 15000 },
    orders: { path: () => '/orders', ms: 10000 },
    research: { path: () => '/research?limit=20', ms: 60000 },
    // The team and the analyst selection come from config/ and change about never,
    // so this is read once a session rather than polled like live state.
    labRoster: { path: () => '/research/roster', ms: 900000 },
    researchJobs: { path: () => '/research/jobs?limit=20', ms: 10000 },
    researchRun: { path: () => activeRun ? '/research/' + encodeURIComponent(activeRun) : null, ms: 5000 },
    proposalDetail: {
      ms: 15000,
      path: () => {
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
  let sessionRequest = null, authEpoch = 0;

  function isLive(key) {
    if (demo) return false;
    if (!key) return wanted.every(k => !SOURCES[k].path() || isLive(k));
    const item = state.resources[key];
    return !!(item && !item.error && item.lastOkAt && Date.now() - item.lastOkAt <= SOURCES[key].ms * 2);
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
      const previous = JSON.stringify(window.CT[key]);
      try {
        const data = await fetchJSON(path);
        // Never attach a late detail response to a newly selected proposal/run.
        if (epoch !== authEpoch || src.path() !== path) return;
        if (key === 'proposalDetail') {
          const current = (window.CT.proposals || []).find(p => p.proposal_id === data.proposal_id);
          if (!current || data.proposal_id !== current.proposal_id || data.version !== current.version) {
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
          const partial = key === 'portfolio' && !!data.buckets && !!data.accounts;
          if (partial) {
            window.CT[key] = data;
            state.resources[key] = { ...(state.resources[key] || {}), receivedAt: Date.now(), error: String(payloadError), path };
            return;
          }
          throw new Error(String(payloadError));
        }
        window.CT[key] = data;
        state.resources[key] = { lastOkAt: Date.now(), error: null, path };
        state.lastOkAt = Date.now();
        if (key === 'proposals') {
          const current = data.find(p => p.proposal_id === pickedProposal) || data[0];
          const detail = window.CT.proposalDetail;
          if (!current || detail && (detail.proposal_id !== current.proposal_id || detail.version !== current.version)) {
            window.CT.proposalDetail = null;
            state.resources.proposalDetail = { lastOkAt: null, error: 'proposal detail needs refresh' };
          }
          if (wanted.includes('proposalDetail')) Promise.resolve().then(() => readOnce('proposalDetail'));
        }
      } catch (error) {
        if (epoch !== authEpoch) return;
        state.resources[key] = { ...(state.resources[key] || {}), error: error.message };
        state.lastError = error.message;
      } finally { announce(previous !== JSON.stringify(window.CT[key])); }
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
  /* Which of several waiting proposals is on screen. Mirrors watchRun: the
     source path reads it, so changing it re-points the same polled resource. */
  function selectProposal(proposalId) {
    if (proposalId === pickedProposal) return pickedProposal;
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
  return { BASE, SOURCES, state, watchRun, selectProposal, fetchJSON, readOnce, start, stop, post, isLive, refresh: reconnect,
    get activeRun() { return activeRun; },
    get pickedProposal() { return pickedProposal; },
    get writesEnabled() { return !demo && !state.authPending && auth.authenticated === true; },
    get authenticated() { return auth.authenticated === true; },
    get username() { return auth.authenticated ? auth.username : null; },
    loaded: keys => demo || keys.every(key => !SOURCES[key].path() || !!(state.resources[key]?.lastOkAt || state.resources[key]?.receivedAt)),
    canAct: keys => !demo && !state.authPending && auth.authenticated === true && keys.every(isLive),
  };
})();
