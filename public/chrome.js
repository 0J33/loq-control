/* ============================================================
   ojee-ui/chrome.js — the interactive half of the design system.

   ojee-ui.css draws the components. This draws the ones that
   cannot be expressed in CSS alone: toasts, a focus-trapped
   modal, a scoped fetch/SSE pair, and a host that mounts a
   module UI and manages its lifecycle.

   Optional. A page that only needs the visual system loads the
   CSS and stops there. Import this when you need behaviour.

   ------------------------------------------------------------
   WHY THIS FILE EXISTS

   The console shell and each module's STANDALONE shell need the
   same runtime: same toast, same modal, same ctx object handed
   to a module's mount(). Left in the console, every module would
   have to reimplement it to run alone — and "runs standalone"
   would quietly become a claim nobody tests.

   Putting it in the design system keeps the dependency arrow
   pointing one way: everything depends on ojee-ui, ojee-ui
   depends on nothing.
   ============================================================ */

/* ── primitives ───────────────────────────────────────────────────────── */

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** <svg class="ic"><use href="#name"></svg> against the page's sprite. */
export const icon = (name, cls = 'ic') =>
  `<svg class="${esc(cls)}" aria-hidden="true"><use href="#${esc(name)}"></use></svg>`;

/** Locale-aware "3 minutes ago" / "in 29 days" from an epoch ms. */
export function relTime(ms) {
  const d = ms - Date.now();
  const abs = Math.abs(d);
  const [unit, size] =
    abs < 60e3 ? ['second', 1e3] :
    abs < 3600e3 ? ['minute', 60e3] :
    abs < 86400e3 ? ['hour', 3600e3] : ['day', 86400e3];
  return new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
    .format(Math.round(d / size), unit);
}

/** Ticks a 24h clock into an element. Returns a stop function. */
export function clock(el, { hour12 = false } = {}) {
  if (!el) return () => {};
  const paint = () => { el.textContent = new Date().toLocaleTimeString('en-GB', { hour12 }); };
  paint();
  const id = setInterval(paint, 1000);
  return () => clearInterval(id);
}

/* ── toast ────────────────────────────────────────────────────────────── */

/**
 * Transient message. Errors linger longer and announce assertively, because
 * an error you did not read is an error that did not happen.
 *
 * Text is set via textContent, never innerHTML — a toast frequently carries a
 * server-supplied error string, which is the classic injection path.
 */
export function toast(kind, title, detail = '', { stack = '#toasts', ms } = {}) {
  const host = typeof stack === 'string' ? document.querySelector(stack) : stack;
  if (!host) return null;

  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.setAttribute('role', kind === 'err' ? 'alert' : 'status');

  const b = document.createElement('b');
  b.textContent = title;
  const s = document.createElement('span');
  s.textContent = detail;
  node.append(b, s);
  host.appendChild(node);

  const life = ms ?? (kind === 'err' ? 7000 : 4200);
  setTimeout(() => {
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 250);
  }, life);
  return node;
}

/* ── modal ────────────────────────────────────────────────────────────── */

/**
 * Focus-trapped dialog. Resolves to the chosen action's `value`, or null if
 * dismissed by Escape or a backdrop click.
 *
 * The trap is the point: a dialog you can Tab out of is a dialog a
 * screen-reader or keyboard user cannot actually use, and focus is returned
 * to whatever opened it so the page does not lose its place.
 */
export function modal({ title, body, actions = [], root = '#modal-root' } = {}) {
  return new Promise((resolve) => {
    const host = typeof root === 'string' ? document.querySelector(root) : root;
    if (!host) return resolve(null);

    const opener = document.activeElement;

    const wrap = document.createElement('div');
    wrap.className = 'modal-backdrop';
    wrap.setAttribute('role', 'dialog');
    wrap.setAttribute('aria-modal', 'true');
    wrap.setAttribute('aria-labelledby', 'ojui-modal-title');
    wrap.innerHTML = `
      <div class="modal">
        <div class="modal-head"><span id="ojui-modal-title"></span></div>
        <div class="modal-body"></div>
        <div class="modal-foot"></div>
      </div>`;
    wrap.querySelector('#ojui-modal-title').textContent = title ?? '';

    const bodyEl = wrap.querySelector('.modal-body');
    if (typeof body === 'string') bodyEl.innerHTML = body;
    else if (body instanceof Node) bodyEl.appendChild(body);

    const foot = wrap.querySelector('.modal-foot');
    let settled = false;
    const close = (value) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      host.innerHTML = '';
      opener?.focus?.();
      resolve(value);
    };

    for (const a of actions) {
      const btn = document.createElement('button');
      btn.className = `btn btn--sm${a.variant ? ` btn--${a.variant}` : ''}`;
      btn.textContent = a.label;
      btn.addEventListener('click', () => close(a.value));
      foot.appendChild(btn);
    }

    const focusables = () => [...wrap.querySelectorAll(
      'button, [href], input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])',
    )].filter((el) => !el.disabled && el.offsetParent !== null);

    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); close(null); return; }
      if (e.key !== 'Tab') return;
      const f = focusables();
      if (!f.length) return;
      const first = f[0];
      const last = f[f.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };

    wrap.addEventListener('click', (e) => { if (e.target === wrap) close(null); });
    document.addEventListener('keydown', onKey, true);

    host.innerHTML = '';
    host.appendChild(wrap);
    (focusables()[0] || wrap.querySelector('.modal')).focus?.();
  });
}

/* ── scoped transport ─────────────────────────────────────────────────── */

/**
 * fetch() scoped to a base path, so a module never hardcodes its own mount
 * point. Mounted, base is `/home`; standalone, base is ''. Same module code.
 *
 * @param {string}   base
 * @param {Function} onUnauthorized  called on a 401 — the host decides whether
 *                                   that means "reload" or "show the login".
 */
export function makeApi(base = '', { onUnauthorized } = {}) {
  return async function api(path, options = {}) {
    const url = `${base}/api${path.startsWith('/') ? path : `/${path}`}`;
    const res = await fetch(url, {
      headers: { 'content-type': 'application/json', ...(options.headers || {}) },
      ...options,
    });

    if (res.status === 401) {
      onUnauthorized?.();
      throw new Error('unauthorized');
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || j.error || detail;
      } catch { /* body was not json */ }
      throw new Error(detail);
    }
    return res.status === 204 ? null : res.json();
  };
}

/**
 * EventSource scoped the same way, with capped exponential backoff.
 *
 * A stream that dies silently and never returns is worse than no stream: the
 * UI keeps showing stale numbers as though they were live. So reconnection is
 * built in, and `onError` is told which attempt it is so the UI can say
 * "reconnecting (3)" rather than pretending everything is fine.
 */
export function makeSse(base = '', registerCleanup = () => {}) {
  return function sse(path, handlers = {}) {
    let source = null;
    let attempt = 0;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      source = new EventSource(`${base}/api${path.startsWith('/') ? path : `/${path}`}`);

      source.onopen = () => { attempt = 0; handlers.onOpen?.(); };
      source.onmessage = (e) => {
        try { handlers.onMessage?.(JSON.parse(e.data), e); }
        catch { handlers.onMessage?.(e.data, e); }
      };
      source.onerror = () => {
        source.close();
        handlers.onError?.(attempt);
        if (stopped) return;
        // Capped, or a module that has been down an hour gets hammered the
        // instant it comes back.
        const wait = Math.min(1000 * 2 ** attempt, 15000);
        attempt += 1;
        setTimeout(connect, wait);
      };
      for (const [name, fn] of Object.entries(handlers.events || {})) {
        source.addEventListener(name, (e) => {
          try { fn(JSON.parse(e.data), e); } catch { fn(e.data, e); }
        });
      }
    };

    connect();
    const stop = () => { stopped = true; source?.close(); };
    registerCleanup(stop);
    return { stop };
  };
}

/* ── module host ──────────────────────────────────────────────────────── */

/**
 * Mounts a module UI and owns its lifecycle. Used identically by the console
 * shell (base = `/{moduleId}`) and by a module's standalone shell (base = '').
 *
 * A module UI default-exports:
 *   { mount(el, ctx), setView?(viewId), unmount?() }
 */
export class ModuleHost {
  /**
   * @param {object}      opts
   * @param {HTMLElement} opts.el          where the module renders
   * @param {string}      opts.base        URL prefix for api/sse ('' standalone)
   * @param {Function}    opts.onUnauthorized
   */
  constructor({ el, base = '', onUnauthorized } = {}) {
    this.el = el;
    this.base = base;
    this.onUnauthorized = onUnauthorized;
    this.cleanups = [];
    this.mounted = null;
    this.manifest = null;
    this.view = null;
  }

  #ctx(manifest, view) {
    const registerCleanup = (fn) => this.cleanups.push(fn);
    return {
      moduleId: manifest?.id || null,
      manifest,
      view,
      base: this.base,
      api: makeApi(this.base, { onUnauthorized: this.onUnauthorized }),
      sse: makeSse(this.base, registerCleanup),
      toast,
      modal,
      icon,
      esc,
      relTime,
      onCleanup: registerCleanup,
      capabilities: manifest?.capabilities || [],
      navigate: (m, v) => { location.hash = `#/${m}${v ? `/${v}` : ''}`; },
    };
  }

  /** A skeleton, not a blank panel — an empty box reads as broken. */
  skeleton() {
    this.el.innerHTML = `<div class="panel stack">
      <div class="skeleton" style="height:18px;width:40%"></div>
      <div class="skeleton" style="height:120px"></div>
      <div class="skeleton" style="height:14px;width:70%"></div>
    </div>`;
  }

  /**
   * @param {object} manifest  the module's module.json
   * @param {string} view      which view to open
   * @param {string} uiUrl     absolute URL of the UI entry point
   */
  async mount(manifest, view, uiUrl) {
    await this.unmount();
    this.manifest = manifest;
    this.view = view;
    this.skeleton();

    try {
      const mod = (await import(uiUrl)).default;
      if (!mod?.mount) throw new Error('module UI has no default export with mount()');
      this.el.innerHTML = '';
      this.mounted = mod;
      await mod.mount(this.el, this.#ctx(manifest, view));
    } catch (e) {
      this.mounted = null;
      this.error(manifest?.name || 'Module', e.message);
      throw e;
    }
  }

  /**
   * Switch view without remounting. Falls back to a remount only if the
   * module did not implement setView — remounting drops its SSE stream, so
   * this is a fallback, not the happy path.
   */
  async setView(view, uiUrl) {
    this.view = view;
    if (this.mounted?.setView) return this.mounted.setView(view);
    if (uiUrl) return this.mount(this.manifest, view, uiUrl);
  }

  async unmount() {
    for (const fn of this.cleanups.splice(0)) {
      try { fn(); } catch { /* a failing cleanup must not block the rest */ }
    }
    if (this.mounted?.unmount) {
      try { await this.mounted.unmount(); } catch (e) { console.warn('unmount failed', e); }
    }
    this.mounted = null;
    if (this.el) this.el.innerHTML = '';
  }

  /** Render a failure with a retry, rather than an empty screen. */
  error(title, detail, { retry } = {}) {
    this.el.innerHTML = `<div class="panel"><div class="empty">
      ${icon('i-warn', 'ic ic--xl')}
      <b>${esc(title)}</b>
      <p>${esc(detail)}</p>
      ${retry === false ? '' : '<button class="btn btn--sm" data-retry>Try again</button>'}
    </div></div>`;
    return this.el.querySelector('[data-retry]');
  }
}

/* ── hash routing ─────────────────────────────────────────────────────── */

/**
 * `#/{a}/{b}` — two levels, so every screen is deep-linkable. Deep links are
 * what make notifications and shared URLs land on the right screen instead of
 * dumping you at the top.
 */
export function parseHash(hash = location.hash) {
  const [a, b] = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  return { a: a || null, b: b || null };
}

/** Calls handler on load and on every hashchange; returns a stop function. */
export function onRoute(handler) {
  const run = () => handler(parseHash());
  window.addEventListener('hashchange', run);
  run();
  return () => window.removeEventListener('hashchange', run);
}

export default {
  esc, icon, relTime, clock, toast, modal,
  makeApi, makeSse, ModuleHost, parseHash, onRoute,
};
