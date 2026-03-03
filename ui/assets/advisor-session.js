/* Tab-scoped writing state only. Never cache account evidence or replay requests. */
(function () {
  'use strict';
  const KEY = 'ct.advisor.session.v1';
  const validId = value => typeof value === 'string' && /^[a-zA-Z0-9_-]{1,64}$/.test(value);
  const short = (value, max) => typeof value === 'string' ? value.slice(0, max) : '';
  function open(owner, binding = 'personal:all') {
    const fresh = () => ({ owner, selected: binding === 'personal:all' ? 'default' : crypto.randomUUID(), entries: Object.create(null), note: null });
    let state = fresh();
    try {
      const envelope = JSON.parse(window.sessionStorage.getItem(KEY));
      const saved = binding === 'personal:all' ? envelope : envelope?.owner === owner ? envelope.scopes?.[binding] : null;
      if (saved?.owner === owner && Date.now() - saved.updatedAt < 86400000) {
        state.selected = validId(saved.selected) ? saved.selected : state.selected;
        for (const [id, entry] of Object.entries(saved.entries || {})) {
          if (!validId(id) || !entry || typeof entry !== 'object') continue;
          state.entries[id] = { draft: short(entry.draft, 4000), title: short(entry.title, 90) };
          if (entry.pending && validId(entry.pending.request_id) && typeof entry.pending.fingerprint === 'string') {
            state.entries[id].pending = { request_id: entry.pending.request_id, fingerprint: entry.pending.fingerprint.slice(0, 10000) };
          }
        }
        if (typeof saved.note?.text === 'string' && Number.isInteger(saved.note.revision) && saved.note.revision >= 0) {
          state.note = { text: short(saved.note.text, 4000), revision: saved.note.revision };
        }
      }
    } catch (_) { /* Unavailable or malformed storage must not prevent writing. */ }
    let last = '';
    function save() {
      try {
        const content = JSON.stringify(state);
        if (content !== last) {
          let old = null;
          try { old = JSON.parse(window.sessionStorage.getItem(KEY) || 'null'); } catch (_) {}
          const envelope = old?.owner === owner ? old : { owner, scopes: {} };
          const saved = { ...state, updatedAt: Date.now() };
          window.sessionStorage.setItem(KEY, JSON.stringify(binding === 'personal:all'
            ? { ...saved, scopes: envelope.scopes || {} }
            : { ...envelope, scopes: { ...envelope.scopes, [binding]: saved } }));
          last = content;
        }
        return true;
      } catch (_) { return false; }
    }
    function clear() {
      state = fresh(); last = '';
      try { window.sessionStorage.removeItem(KEY); } catch (_) {}
    }
    return { get state() { return state; }, save, clear, validId };
  }
  window.AdvisorSession = { open };
})();
