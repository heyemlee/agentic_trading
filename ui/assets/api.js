/* Same-origin API with authenticated writes and independent freshness per resource. */
window.CT_API = (function () {
  'use strict';
  const BASE = '';
  const demo = window.CT_DEMO === true;
  const TIMEOUT_MS = 15000;
  let activeRun = null;
  let wanted = [];
  let auth = { authenticated: false, csrf_token: null };
  const SOURCES = {
    health: { path: () => '/health', ms: 30000 },
    schedule: { path: () => '/system/schedule', ms: 60000 },
    operations: { path: () => '/system/operations', ms: 60000 },
    notifications: { path: () => '/system/notifications', ms: 120000 },
    portfolio: { path: () => '/portfolio', ms: 60000 },
    risk: { path: () => '/risk', ms: 60000 },
    microLive: { path: () => '/micro-live', ms: 60000 },
    proposals: { path: () => '/proposals', ms: 15000 },
    orders: { path: () => '/orders', ms: 10000 },
    research: { path: () => '/research?limit=20', ms: 60000 },
    researchJobs: { path: () => '/research/jobs?limit=20', ms: 10000 },
    researchRun: { path: () => activeRun ? '/research/' + encodeURIComponent(activeRun) : null, ms: 5000 },
    proposalDetail: {
      ms: 15000,
      path: () => {
        const open = (window.CT.proposals || [])[0];
        return open ? '/proposals/' + encodeURIComponent(open.proposal_id) : null;
      },
    },
  };
  const state = { online: null, demo, resources: {}, live: {}, lastOkAt: null, lastError: null };
  const timers = [];
  const inFlight = new Map();

  function isLive(key) {
    if (demo) return false;
    if (!key) return wanted.every(k => !SOURCES[k].path() || isLive(k));
    const item = state.resources[key];
    return !!(item && !item.error && item.lastOkAt && Date.now() - item.lastOkAt <= SOURCES[key].ms * 2);
  }
  function announce() {
    state.online = !demo && wanted.length > 0 && isLive();
    Object.keys(SOURCES).forEach(key => { state.live[key] = isLive(key); });
    document.dispatchEvent(new CustomEvent('ct:connection', { detail: state }));
    document.dispatchEvent(new CustomEvent('ct:data', { detail: state }));
  }
  function unauthorized() {
    auth = { authenticated: false, csrf_token: null };
    Object.keys(state.resources).forEach(key => { state.resources[key].error = 'login required'; });
    document.dispatchEvent(new CustomEvent('ct:auth', { detail: auth }));
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
      try {
        const data = await fetchJSON(path);
        // Never attach a late detail response to a newly selected proposal/run.
        if (src.path() !== path) return;
        if (key === 'proposalDetail') {
          const current = (window.CT.proposals || [])[0];
          if (!current || data.proposal_id !== current.proposal_id || data.version !== current.version) {
            throw new Error('Proposal changed while its detail was loading. Refreshing the queue.');
          }
        }
        const payloadError = data && !Array.isArray(data) &&
          (data.error || data.errors && Object.keys(data.errors).length && JSON.stringify(data.errors));
        if (payloadError) {
          // Orders include local records even if the venue is unavailable. Keep those
          // records visible while explicitly marking the resource as incomplete.
          if (key === 'orders' && Array.isArray(data.fills) && Array.isArray(data.open_orders)) {
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
          const current = data[0];
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
      } finally { announce(); }
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
    try {
      auth = await fetchJSON('/auth/session');
      if (!auth.authenticated) unauthorized();
    } catch (e) { unauthorized(); }
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
    timers.push(setInterval(announce, 1000));
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !demo) { loadSession(); wanted.forEach(readOnce); }
  });
  async function post(path, body) {
    if (demo) return { blocked: true, reason: 'Demo mode cannot change state.' };
    if (!auth.authenticated || !auth.csrf_token) throw new Error('Sign in before performing this action.');
    return request(path, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'X-CT-Request': '1', 'X-CSRF-Token': auth.csrf_token,
    }, body: JSON.stringify(body || {}) }, 60000);
  }
  function watchRun(runKey) {
    activeRun = runKey || null;
    window.CT.researchRun = null;
    if (activeRun) readOnce('researchRun');
    return activeRun;
  }
  return { BASE, SOURCES, state, watchRun, fetchJSON, readOnce, start, stop, post, isLive,
    get activeRun() { return activeRun; },
    get writesEnabled() { return !demo && auth.authenticated === true; },
    get authenticated() { return auth.authenticated === true; },
    loaded: keys => demo || keys.every(key => !SOURCES[key].path() || !!(state.resources[key]?.lastOkAt || state.resources[key]?.receivedAt)),
    canAct: keys => !demo && auth.authenticated === true && keys.every(isLive),
  };
})();
