window.Console.page('profile', function (document) {
  'use strict';
  /* The personal context used to be a sheet borrowed from the Assistant page,
     so closing it left you on the Assistant — a page you never asked for. It
     is its own page now, and it goes back where you came from.

     The Assistant keeps its own copy of this sheet for in-conversation edits;
     both write the same /advisor/notes record, guarded by its revision. */
  const C = window.Console, D = C.D;
  C.mount('profile');

  const MAX = 4000;
  /* Topics are a way in, not a form: a tap writes "Topic:" on a new line, or
     goes to the line that already starts with it. The record stays one text. */
  const TOPICS = ['goals', 'timeline', 'income', 'plans', 'style'];
  const DEVICES = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="5" width="13" height="9.5" rx="1.5"/>' +
    '<path d="M1.5 18h15"/><rect x="17.5" y="8.5" width="5" height="10.5" rx="1.3"/></svg>';

  const $ = (id) => document.getElementById(id);
  const notes = $('profile-notes'), save = $('profile-save'), discard = $('profile-discard');
  const status = $('profile-status'), state = $('profile-state'), count = $('profile-count');
  const savedAt = $('profile-saved-at'), topics = $('profile-topics'), hint = $('profile-hint');

  document.querySelectorAll('[data-pc-icon]').forEach((el) => {
    el.innerHTML = el.dataset.pcIcon === 'devices' ? DEVICES : (C.ICON[el.dataset.pcIcon] || '');
  });

  /* baseline is the saved text the editor started from; the editor holds
     unsaved changes exactly when it differs from it. */
  let baseline = '', revision = 0, updatedAt = null, loaded = false, saving = false;
  let newer = false, topic = null, topicsLang = null;
  /* The highest revision this page wrote. A poll that left before the save answers
     still carries the one before it, and must neither restore that text nor read
     as another device's edit. */
  let written = 0;
  const changed = () => loaded && notes.value !== baseline;

  /* ─── topics ─────────────────────────────────────────────────────── */
  const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  /* A heading written while the console spoke the other language still counts. */
  const labels = (key) => [...new Set(window.I18N.variants
    ? window.I18N.variants('pc.topic.' + key) : [C.t('pc.topic.' + key)])];
  function find(text, key) {
    const match = new RegExp('^[ \\t]*(?:' + labels(key).map(escapeRe).join('|') + ')[ \\t]*[:：]', 'im').exec(text);
    return match ? match.index : -1;
  }
  function paintTopics() {
    if (topicsLang !== window.I18N.lang) {
      topicsLang = window.I18N.lang;
      C.setHTML(topics, TOPICS.map((key) => '<button class="pc-topic" type="button" data-topic="' + key + '">' +
        '<span class="pc-topic-mark" aria-hidden="true"></span><span>' +
        C.escapeHtml(C.t('pc.topic.' + key)) + '</span></button>').join(''));
    }
    topics.querySelectorAll('[data-topic]').forEach((chip) => {
      const key = chip.dataset.topic, covered = find(notes.value, key) >= 0;
      if (chip.dataset.covered === String(covered) && chip.dataset.lang === topicsLang) return;
      chip.dataset.covered = String(covered);
      chip.dataset.lang = topicsLang;
      chip.classList.toggle('is-covered', covered);
      chip.setAttribute('aria-label', C.t(covered ? 'pc.topicGo' : 'pc.topicAdd', { topic: C.t('pc.topic.' + key) }));
      chip.querySelector('.pc-topic-mark').innerHTML = covered ? C.ICON.check : C.ICON.plus;
    });
    hint.hidden = !topic;
    C.setHTML(hint, topic ? '<strong>' + C.escapeHtml(C.t('pc.topic.' + topic)) + '</strong> · ' +
      C.escapeHtml(C.t('pc.hint.' + topic)) : '');
  }
  function goTo(key) {
    const text = notes.value, at = find(text, key);
    let caret;
    if (at >= 0) {
      const end = text.indexOf('\n', at);
      caret = end < 0 ? text.length : end;
    } else {
      const heading = (text && !text.endsWith('\n') ? '\n' : '') + C.t('pc.topic.' + key) + C.t('pc.topicSep');
      if (text.length + heading.length > MAX) {
        status.className = 'pc-status is-error';
        status.textContent = C.t('pc.full');
        return;
      }
      notes.value = text + heading;
      caret = notes.value.length;
    }
    topic = key;
    notes.focus();
    notes.setSelectionRange(caret, caret);
    paint();
  }
  topics.addEventListener('click', (event) => {
    const chip = event.target.closest && event.target.closest('[data-topic]');
    if (chip) goTo(chip.dataset.topic);
  });

  /* ─── the editor ─────────────────────────────────────────────────── */
  /* The text grows with its content, so the page scrolls and the box never does. */
  function fit() {
    const y = window.scrollY;
    notes.style.height = 'auto';
    if (notes.scrollHeight) notes.style.height = notes.scrollHeight + 'px';
    if (y !== undefined && window.scrollY !== y) window.scrollTo(0, y);
  }
  function paint() {
    const n = notes.value.length, dirty = changed();
    count.textContent = C.t('pc.count', { n: n.toLocaleString('en-US'), max: MAX.toLocaleString('en-US') });
    count.className = 'pc-count num' + (n >= MAX ? ' is-full' : n >= MAX * 0.9 ? ' is-near' : '');
    state.textContent = saving ? C.t('pc.saving') : dirty ? C.t('pc.unsaved')
      : !loaded ? '' : baseline ? C.t('pc.saved') : C.t('pc.empty');
    state.className = 'pc-state' + (saving ? '' : dirty ? ' is-dirty' : baseline ? ' is-saved' : '');
    savedAt.textContent = updatedAt ? C.t('pc.savedAt', { at: C.dateTime(updatedAt) }) : '';
    save.disabled = saving || !dirty || !CT_API.writesEnabled;
    discard.hidden = !dirty;
    discard.disabled = saving;
    paintTopics();
    fit();
  }

  function render() {
    if (!window.CT_DEMO && !CT_API.authenticated) {
      notes.value = ''; status.textContent = ''; status.className = 'pc-status';
      baseline = ''; revision = 0; updatedAt = null; written = 0;
      loaded = false; newer = false; topic = null;
      paint();
      return;
    }
    const saved = (D.advisor || {}).notes;
    /* Never clobber something the reader is in the middle of typing. */
    if (saved && !changed() && (!loaded || (saved.revision !== revision && saved.revision >= written))) {
      notes.value = saved.text;
      baseline = saved.text;
      revision = saved.revision;
      updatedAt = saved.updated_at || null;
      loaded = true;
    }
    /* The demo has no saved record; its editor starts empty and refuses to save. */
    if (window.CT_DEMO && !saved) loaded = true;
    /* Another device saved while this one has edits: say so before a save is refused. */
    const wasNewer = newer;
    newer = !!saved && changed() && saved.revision > revision;
    if (newer && !wasNewer) {
      status.className = 'pc-status is-error';
      status.textContent = C.t('pc.newer');
    }
    paint();
  }

  notes.addEventListener('input', () => {
    /* "Saved" stops being true with the next keystroke; an error stays until acted on. */
    if (!status.className.includes('is-error')) status.textContent = '';
    paint();
  });
  notes.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === 's') {
      event.preventDefault();
      save.click();
    }
  });

  save.addEventListener('click', async () => {
    if (save.disabled) return;
    const text = notes.value;
    saving = true;
    status.className = 'pc-status';
    status.textContent = '';
    paint();
    try {
      const result = await CT_API.post('/advisor/notes', { text, revision });
      if (result.blocked) { status.textContent = C.t('pc.demo'); return; }
      revision = result.revision;
      written = Math.max(written, result.revision);
      updatedAt = result.updated_at || updatedAt;
      /* The server trims what it stores. Show that, unless they typed on during the round trip. */
      baseline = typeof result.text === 'string' ? result.text : text;
      if (notes.value === text) notes.value = baseline;
      newer = false; topic = null;
      status.textContent = C.t('ai.saved');
      CT_API.readOnce('advisor');
    } catch (error) {
      status.className = 'pc-status is-error';
      status.textContent = C.t('ai.error', { error: error.message }) +
        (error.status === 409 ? ' ' + C.t('pc.conflictHint') : '');
    } finally {
      saving = false;
      paint();
    }
  });

  /* Back to what is saved — and, after another device saved, to its version. */
  discard.addEventListener('click', async () => {
    if (discard.disabled) return;
    notes.value = baseline;
    newer = false; topic = null;
    status.className = 'pc-status';
    status.textContent = '';
    render();
    if (!window.CT_DEMO) {
      try { await CT_API.readOnce('advisor'); } catch (error) { /* the poll will bring it */ }
    }
  });

  document.addEventListener('ct:auth', render);
  C.live(['advisor'], render);
});
