/* Console pages share one document and one API client. Only static templates
   are prefetched; account data and retained DOM live in this tab's memory only. */
(function () {
  'use strict';
  const routes = new Set(['index', 'portfolio', 'advisor', 'more', 'risk', 'activity',
    'evaluation', 'profile', 'notifications', 'system', 'approvals', 'research', 'lab', 'settings']);
  const dependencies = { advisor: ['advisor-session.js'], notifications: ['push.js'],
    approvals: ['approval-intent.js'], lab: ['lab/pixel-office.js'] };
  const views = new Map(), factories = new Map(), pending = new Map();
  const assets = new Map([...document.querySelectorAll('script[src], link[rel="stylesheet"]')]
    .map(el => [el.src || el.href, Promise.resolve()]));
  const base = new URL('./', location.href);
  const version = new URL(document.currentScript.src).search;
  const routeName = url => url.pathname.slice(base.pathname.length).replace(/\.html$/, '') || 'index';
  let active, revision = 0;
  history.scrollRestoration = 'manual';
  history.replaceState({ ...history.state, ctDepth: 0 }, '');

  function eligible(url) {
    return url.origin === base.origin && url.pathname.startsWith(base.pathname) &&
      routes.has(routeName(url)) && (url.searchParams.get('demo') === '1') === !!window.CT_DEMO &&
      [...url.searchParams.keys()].every(key => ['demo', 'view', 'sheet', 'prompt', 'ticker', 'run'].includes(key));
  }
  function makeView(name, body, title) {
    const storage = document.createElement('div');
    const nodes = [...body.children].filter(el => !['SCRIPT', 'TEMPLATE'].includes(el.tagName) && el.id !== 'startup');
    return { name, storage, nodes, title, className: body.className, scroll: 0, initialized: false, keys: [] };
  }
  active = makeView(routeName(new URL(location.href)), document.body, document.title);
  views.set(active.name, active);
  for (const template of document.querySelectorAll('template[data-ct-page]')) {
    const view = makeView(template.dataset.ctPage,
      { children: template.content.children, className: template.dataset.class }, template.dataset.title);
    view.storage.append(...view.nodes);
    views.set(view.name, view);
    template.remove();
  }

  // Page callbacks keep their own DOM, even when a request completes after leaving.
  // Inactive pages receive auth invalidation, but never poll or handle another tab's clicks.
  function pageDocument(view) {
    return new Proxy(document, {
      get(target, key) {
        if (key === 'hidden') return target.hidden || active !== view;
        if (key === 'body') return active === view ? target.body : view.storage;
        if (key === 'getElementById') return id => active === view
          ? target.getElementById(id) : view.storage.querySelector('#' + CSS.escape(id));
        if (key === 'querySelector' || key === 'querySelectorAll') return selector =>
          (active === view ? target : view.storage)[key](selector);
        if (key === 'addEventListener') return (type, fn, options) => target.addEventListener(type, event => {
          if (active === view || type === 'ct:auth' ||
              (type === 'ct:leave' && event.detail?.name === view.name)) fn(event);
        }, options);
        const value = Reflect.get(target, key, target);
        return typeof value === 'function' ? value.bind(target) : value;
      },
    });
  }
  function initialize(view) {
    if (view.initialized) return;
    const init = factories.get(view.name);
    if (!init) {
      Console.mount(view.name === 'index' ? 'dashboard' : view.name);
      const content = document.getElementById('content');
      content.hidden = false;
      content.inert = true;
      Console.loading(true);
      CT_API.start(['health', 'proposals']);
      return;
    }
    document.getElementById('content').inert = false;
    view.initialized = true;
    init(pageDocument(view));
  }
  window.CT_NAV = {
    register(name, init) {
      factories.set(name, init);
      if (active.name === name) initialize(active);
    },
    bind(keys) { active.keys = keys; },
  };

  function asset(url, stylesheet) {
    if (!assets.has(url)) {
      const promise = new Promise((resolve, reject) => {
        const el = document.createElement(stylesheet ? 'link' : 'script');
        if (stylesheet) { el.rel = 'stylesheet'; el.href = url; }
        else { el.src = url; el.async = false; }
        el.onload = resolve;
        el.onerror = () => { assets.delete(url); el.remove(); reject(new Error('Asset unavailable')); };
        document.head.append(el);
      });
      assets.set(url, promise);
    }
    return assets.get(url);
  }
  function prepare(name) {
    if (views.has(name)) {
      return (async () => {
        for (const dependency of dependencies[name] || []) {
          await asset(new URL('assets/' + dependency + version, base).href, false);
        }
        await asset(new URL('assets/pages/' + name + '.js' + version, base).href, false);
        return views.get(name);
      })();
    }
    if (pending.has(name)) return pending.get(name);
    const url = new URL(name + '.html', base);
    if (window.CT_DEMO) url.searchParams.set('demo', '1');
    const task = (async () => {
      const res = await fetch(url, { credentials: 'same-origin', cache: 'no-cache' });
      if (!res.ok || res.redirected) throw new Error('Page unavailable');
      const html = new DOMParser().parseFromString(await res.text(), 'text/html');
      if (!html.getElementById('content')) throw new Error('Missing page content');
      const shared = html.querySelector('script[src*="assets/console.js"]');
      if (!shared || !assets.has(new URL(shared.getAttribute('src'), url).href)) {
        throw new Error('Release changed; load the complete document');
      }
      const css = [...html.querySelectorAll('link[rel="stylesheet"]')]
        .map(el => asset(new URL(el.getAttribute('href'), url).href, true));
      // Shared infrastructure is already running. Dependencies and page factories
      // execute once; prefetch never mounts a page or starts its automatic refresh job.
      for (const el of html.querySelectorAll('script[src]')) {
        const src = el.getAttribute('src');
        if (/assets\/(?:pages\/|advisor-session\.js|push\.js|approval-intent\.js|lab\/pixel-office\.js)/.test(src)) {
          await asset(new URL(src, url).href, false);
        }
      }
      await Promise.all(css);
      const view = makeView(name, html.body, html.title);
      view.storage.append(...view.nodes);
      views.set(name, view);
      return view;
    })().finally(() => pending.delete(name));
    pending.set(name, task);
    return task;
  }
  function show(view, url, historyMode) {
    if (historyMode === 'push') history.pushState({ ctDepth: (history.state?.ctDepth || 0) + 1 }, '', url);
    if (active !== view) {
      document.dispatchEvent(new CustomEvent('ct:leave', { detail: { name: active.name } }));
      document.querySelectorAll('dialog[open]').forEach(el => el.close());
      active.className = document.body.className;
      active.scroll = window.scrollY;
      active.storage.append(...active.nodes);
      document.getElementById('blank-state')?.remove();
      active = view;
      document.body.className = view.className;
      const foot = document.querySelector('.pagefoot');
      for (const node of view.nodes) document.body.insertBefore(node, foot);
      document.title = view.title;
      if (view.initialized) {
        Console.mount(view.name === 'index' ? 'dashboard' : view.name);
        CT_API.start(view.keys);
      } else initialize(view);
      I18N.applyStatic();
      window.scrollTo(0, view.scroll);
    }
    document.dispatchEvent(new CustomEvent('ct:route'));
    document.dispatchEvent(new CustomEvent('ct:data'));
    if (url.hash) document.getElementById(decodeURIComponent(url.hash.slice(1)))?.scrollIntoView();
    performance.mark('ct:navigation-visible');
  }
  function navigate(url, historyMode = 'push') {
    const ticket = ++revision;
    if (routeName(url) === 'advisor' && url.searchParams.get('view') === 'holdings') {
      url = new URL('portfolio.html' + (window.CT_DEMO ? '?demo=1' : ''), base);
    }
    const name = routeName(url), view = views.get(name);
    // This path is synchronous: a prepared tab never waits for a request or microtask.
    if (view) {
      show(view, url, historyMode);
      if (!view.initialized) prepare(name).catch(() => {
        if (ticket === revision) location.assign(url.href);
      });
      return;
    }
    prepare(name).then(view => {
      if (ticket === revision) show(view, url, historyMode);
    }).catch(() => {
      if (ticket === revision) location.assign(url.href);
    });
  }
  document.addEventListener('click', event => {
    if (event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const url = new URL(link.href, location.href);
    if (window.CT_DEMO && url.origin === base.origin && routes.has(routeName(url))) url.searchParams.set('demo', '1');
    if (!eligible(url)) return;
    event.preventDefault();
    if (link.id === 'back-link' && history.state?.ctDepth > 0) {
      revision++;
      history.back();
      return;
    }
    if (url.href === location.href) { revision++; return; }
    performance.mark('ct:navigation-click');
    navigate(url);
  });
  window.addEventListener('popstate', () => {
    const url = new URL(location.href);
    if (eligible(url)) navigate(url, 'pop');
    else location.reload();
  });
  // Download page factories without mounting hidden pages or issuing business requests.
  for (const name of routes) if (name !== active.name) prepare(name).catch(() => {});
  let warmed = false;
  document.addEventListener('ct:auth', () => {
    if (warmed || !CT_API.authenticated) return;
    warmed = true;
    for (const key of ['portfolio', 'advisor']) if (!CT_API.isLive(key)) CT_API.readOnce(key);
  });
})();
