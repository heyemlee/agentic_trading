/* ─────────────────────────────────────────────────────────────────────────
 * Conservative Trading console — live data layer.
 *
 * Reads http://127.0.0.1:8787 on the polling cadence fixed in
 * FRONTEND_HANDOFF.md §15. There is no websocket and none is needed; the
 * fastest thing that moves is a quote, and this console never shows one
 * outside an approval check.
 *
 * Degradation contract: when the backend is unreachable the sanitized
 * fixtures already in window.CT stay on screen and the UI says so loudly.
 * It must never be possible to mistake fixture data for live data.
 *
 * WRITES ARE OFF BY DEFAULT. Approve / reject / pause are real money and
 * real state; a design prototype must not be able to fire them by accident.
 * Set CT_API.writesEnabled = true only in the built app.
 *
 * Same-origin note: the API declares no CORS middleware, so a page served
 * from any other origin has every request blocked by the browser. Serve this
 * console from the API itself (see the note in the delivery summary) or add
 * CORSMiddleware on the backend.
 * ───────────────────────────────────────────────────────────────────── */

window.CT_API = (function () {
  'use strict';

  const BASE = 'http://127.0.0.1:8787';
  const TIMEOUT_MS = 4000;

  /* key → where it comes from and how often, per handoff §15.
     `key` matches the property name on window.CT so a successful read can be
     written straight back onto the object every page already reads from. */
  const SOURCES = {
    health:     { path: () => '/health',            ms: 30000 },
    portfolio:  { path: () => '/portfolio',         ms: 60000 },
    risk:       { path: () => '/risk',              ms: 60000 },
    microLive:  { path: () => '/micro-live',        ms: 60000 },
    proposals:  { path: () => '/proposals',         ms: 15000 },
    orders:     { path: () => '/orders',            ms: 10000 },
    research:   { path: () => '/research?limit=20', ms: 60000 },
    /* A research run in flight. The backend streams the agent's own stdout as
       `log_tail`, so this is the live view of what the agent is doing. */
    researchRun: {
      ms: 5000,
      path: () => (activeRun ? '/research/' + encodeURIComponent(activeRun) : null),
    },
    /* one open proposal at a time; the detail follows whatever is in the queue */
    proposalDetail: {
      ms: 15000,
      clearWhenAbsent: true,
      path: () => {
        const open = (window.CT.proposals || [])[0];
        return open ? '/proposals/' + encodeURIComponent(open.proposal_id) : null;
      },
    },
  };

  let activeRun = null;              /* run_key of the run being watched */

  const state = {
    online: null,          /* null = not tried yet, true = live, false = fixture */
    lastOkAt: null,
    lastErrorAt: null,
    lastError: null,
    live: {},              /* key → true once that key has come from the API */
  };

  const timers = [];
  let announced = null;

  function announce() {
    if (state.online !== announced) {
      announced = state.online;
      document.dispatchEvent(new CustomEvent('ct:connection', { detail: state }));
    }
    document.dispatchEvent(new CustomEvent('ct:data', { detail: state }));
  }

  async function fetchJSON(path) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(BASE + path, {
        signal: ctrl.signal,
        headers: { Accept: 'application/json' },
      });
      if (!res.ok) {
        let detail = res.status + ' ' + res.statusText;
        try { const body = await res.json(); if (body && body.detail) detail = body.detail; } catch (e) {}
        throw new Error(detail);
      }
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function readOnce(key) {
    const src = SOURCES[key];
    if (!src) return;
    const path = src.path();
    if (!path) {
      /* Nothing to ask for. If we are live, whatever is cached under this key
         is a fixture and must go — otherwise it reads as real data. */
      if (src.clearWhenAbsent && state.online === true && window.CT[key] != null) {
        window.CT[key] = null;
        announce();
      }
      return;
    }
    try {
      const data = await fetchJSON(path);
      window.CT[key] = data;               /* mutate in place — pages hold this object */
      state.live[key] = true;
      state.online = true;
      state.lastOkAt = Date.now();
      state.lastError = null;
    } catch (err) {
      state.online = false;
      state.lastErrorAt = Date.now();
      state.lastError = err && err.name === 'AbortError'
        ? 'the request timed out after ' + (TIMEOUT_MS / 1000) + 's'
        : (err && err.message) || 'the request failed';
    }
    announce();
  }

  /* Start polling only what the current screen actually reads. */
  function start(keys) {
    stop();
    const wanted = (keys || []).filter((k) => SOURCES[k]);
    if (!wanted.length) return;
    wanted.forEach((key) => {
      readOnce(key);
      timers.push(setInterval(() => readOnce(key), SOURCES[key].ms));
    });
    /* Stop burning requests while the tab is hidden; catch up on return. */
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) wanted.forEach(readOnce);
    });
  }

  function stop() {
    while (timers.length) clearInterval(timers.pop());
  }

  /* Writes. Deliberately inert until the built app opts in. */
  async function post(path, body) {
    if (!api.writesEnabled) {
      console.warn('[CT_API] write blocked — CT_API.writesEnabled is false:', path, body);
      return { blocked: true, reason: 'writes are disabled in this build' };
    }
    const res = await fetch(BASE + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.status + ' ' + res.statusText);
    return data;
  }

  /* Point the researchRun poller at a run and read it immediately. */
  function watchRun(runKey) {
    activeRun = runKey || null;
    window.CT.researchRun = null;
    if (activeRun) readOnce('researchRun');
    return activeRun;
  }

  const api = {
    BASE, SOURCES, state, watchRun,
    get activeRun() { return activeRun; },
    writesEnabled: false,
    fetchJSON, readOnce, start, stop, post,
    isLive: (key) => (key ? !!state.live[key] : state.online === true),
  };
  return api;
})();
