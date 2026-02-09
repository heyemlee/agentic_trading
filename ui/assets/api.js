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
  const SOURCES = {
    advisor: { path: () => '/advisor', ms: 5000 },
    health: { path: () => '/health', ms: 30000 },
    schedule: { path: () => '/system/schedule', ms: 60000 },
    operations: { path: () => '/system/operations', ms: 60000 },
    notifications: { path: () => '/system/notifications', ms: 120000 },
    pushDevices: { path: () => '/notifications/devices', ms: 120000 },
    authSessions: { path: () => '/auth/sessions', ms: 60000 },
    evaluation: { path: () => '/evaluation', ms: 300000 },
    portfolio: { path: () => '/portfolio', ms: 15000 },
    risk: { path: () => '/risk', ms: 15000 },
    microLive: { path: () => '/micro-live', ms: 15000 },
    proposals: { path: () => '/proposals', ms: 5000 },
    orders: { path: () => '/orders', ms: 10000 },
    research: { path: () => '/research?limit=20', ms: 10000 },
    // The team and the analyst selection come from config/ and change about never,
    // so this is read once a session rather than polled like live state.
    labRoster: { path: () => '/research/roster', ms: 900000 },
    researchJobs: { path: () => '/research/jobs?limit=20', ms: 5000 },
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
  const state = { online: null, demo, authPending: !demo, resources: {}, live: {}, lastOkAt: null, lastError: null };
  const timers = [];
  const inFlight = new Map();

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
    const signature = JSON.stringify([state.authPending, auth.authenticated, state.live,
      Object.entries(state.resources).map(([key, item]) => [key, item.error])]);
    if (changed || signature !== connectionSignature) {
      connectionSignature = signature;
      document.dispatchEvent(new CustomEvent('ct:data', { detail: state }));
    }
  }
  function unauthorized(clearDrafts = true) {
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
        if (res.status === 401 || res.status === 503 && path === '/auth/session') unauthorized();
        throw new Error(typeof body.detail === 'string' ? body.detail : res.status + ' ' + res.statusText);
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
      const previous = JSON.stringify(window.CT[key]);
      try {
        const data = await fetchJSON(path);
        // Never attach a late detail response to a newly selected proposal/run.
        if (src.path() !== path) return;
        if (key === 'proposalDetail') {
          const current = (window.CT.proposals || []).find(p => p.proposal_id === data.proposal_id);
          if (!current || data.proposal_id !== current.proposal_id || data.version !== current.version) {
            throw new Error('Proposal changed while its detail was loading. Refreshing the queue.');
          }
        }
        const payloadError = data && !Array.isArray(data) &&
          (data.error || data.errors && Object.keys(data.errors).length && JSON.stringify(data.errors));
        if (payloadError) {
          /* Two payloads carry their own failure and are still worth reading.
             Orders include local records even if the venue is unavailable. A ledger
             that has gone unconfirmed answers with its buckets emptied and the reason
             in errors, and the pages are written for exactly that shape — a gap and the
             reason, never a zero. Throwing it away instead leaves the reader on a blank
             screen that cannot say why or offer the way out. Both stay marked incomplete,
             so canAct() still refuses and nothing may be approved against them. */
          const partial = key === 'orders' ? Array.isArray(data.fills) && Array.isArray(data.open_orders)
                        : key === 'portfolio' ? !!data.buckets && !!data.accounts
                        : false;
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
    state.authPending = true;
    try {
      auth = await fetchJSON('/auth/session');
      if (!auth.authenticated) unauthorized();
    } catch (e) { unauthorized(false); }
    state.authPending = false;
    document.dispatchEvent(new CustomEvent('ct:auth', { detail: auth }));
  }
  function start(keys) {
    stop();
    wanted = [...new Set((keys || []).filter(key => SOURCES[key]))];
    wanted.forEach(key => { state.resources[key] ||= { lastOkAt: null, error: 'loading' }; });
    if (wanted.includes('proposalDetail') && !wanted.includes('proposals')) wanted.push('proposals');
    if (demo) { announce(); return; }
    loadSession().then(() => {
      wanted.forEach(key => { if (key !== 'proposalDetail') readOnce(key); });
    });
    wanted.forEach(key => timers.push(setInterval(() => { if (!document.hidden) readOnce(key); }, SOURCES[key].ms)));
    timers.push(setInterval(() => { if (!document.hidden) announce(); }, 1000));
  }
  async function reconnect() {
    if (document.hidden || demo) return;
    await loadSession();
    wanted.forEach(readOnce);
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
    if (!auth.authenticated || !auth.csrf_token) throw new Error('Sign in before performing this action.');
    const result = await request(path, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-CT-Request': '1', 'X-CSRF-Token': auth.csrf_token,
    }, body: JSON.stringify(body || {}) }, 60000);
    if (path === '/auth/logout') unauthorized();
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
  return { BASE, SOURCES, state, watchRun, selectProposal, fetchJSON, readOnce, start, stop, post, isLive,
    get activeRun() { return activeRun; },
    get pickedProposal() { return pickedProposal; },
    get writesEnabled() { return !demo && auth.authenticated === true; },
    get authenticated() { return auth.authenticated === true; },
    get username() { return auth.authenticated ? auth.username : null; },
    loaded: keys => demo || keys.every(key => !SOURCES[key].path() || !!(state.resources[key]?.lastOkAt || state.resources[key]?.receivedAt)),
    canAct: keys => !demo && auth.authenticated === true && keys.every(isLive),
  };
})();
