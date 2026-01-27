(function () {
  /* The personal context used to be a sheet borrowed from the Assistant page,
     so closing it left you on the Assistant — a page you never asked for. It
     is its own page now, and it goes back where you came from.

     The Assistant keeps its own copy of this sheet for in-conversation edits;
     both write the same /advisor/notes record, guarded by its revision. */
  const C = window.Console, D = C.D;
  C.mount('profile');

  const notes = document.getElementById('profile-notes');
  const save = document.getElementById('profile-save');
  const status = document.getElementById('profile-status');

  let revision = 0, loaded = false, dirty = false;

  function render() {
    const saved = (D.advisor || {}).notes;
    /* Never clobber something the reader is in the middle of typing. */
    if (saved && !dirty && (!loaded || saved.revision !== revision)) {
      notes.value = saved.text;
      revision = saved.revision;
      loaded = true;
    }
    save.disabled = !CT_API.writesEnabled;
  }

  notes.addEventListener('input', () => { dirty = true; });

  save.addEventListener('click', async () => {
    if (save.disabled) return;
    save.disabled = true;
    status.textContent = '';
    const text = notes.value;
    try {
      const result = await CT_API.post('/advisor/notes', { text, revision });
      if (result.blocked) { status.textContent = C.t('ai.demo'); return; }
      revision = result.revision;
      dirty = notes.value !== text;          /* they may have typed on during the round trip */
      status.textContent = C.t('ai.saved');
      CT_API.readOnce('advisor');
    } catch (error) {
      status.textContent = C.t('ai.error', { error: error.message });
    } finally {
      save.disabled = !CT_API.writesEnabled;
    }
  });

  document.addEventListener('ct:auth', render);
  C.live(['advisor'], render);
})();
