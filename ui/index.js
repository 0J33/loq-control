/* ============================================================
   ojee-loq — module UI. READ ONLY.

   Two views over one telemetry stream: Monitor and Battery.

   This module shows the machine; it does not drive it. Controls
   live in the PyQt desktop app, which runs at the machine with
   a person sitting at it.

   That split is deliberate. Three surfaces — the desktop app,
   the browser on a desktop, the browser on a phone — had drifted
   into three different subsets of the same controls, which is
   worse than one surface having them and one not. It also means
   the auto-revert, the confirm dialogs and the privileged write
   paths no longer need to be reachable over the tailnet.

   The agent still SERVES /api/control, /api/revert and /api/kill
   for the desktop app; this UI simply never calls them.

   Design points that are not obvious:

   * The skeleton is built ONCE per view and repainted by id.
     Re-rendering innerHTML on every SSE tick would restart the
     sparklines and flicker every readout once a second.

   * Readouts use tabular figures. Proportional digits re-flow the
     row as values change, and a number that dances is harder to
     read than one that is still.
   ============================================================ */

let ctx = null;
let root = null;
let view = 'monitor';

let snap = null;
let caps = {};

const HIST = 90;              // ~90s of history at one sample a second
const hist = { cpuTemp: [], gpuTemp: [], cpuUsage: [], gpuUsage: [], cpuW: [], gpuW: [] };

/* ── helpers ──────────────────────────────────────────────────────────── */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const $ = (sel) => root?.querySelector(sel);
const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));

function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
const fmtRate = (bps) => `${fmtBytes(bps)}/s`;
/* Splits "20.5 KB/s" into ["20.5", "KB/s"] so the number renders at .value
   size and the unit in the <sup>, like every other tile. Baked together it
   was one long string that wrapped onto a second line. */
const splitRate = (bps) => { const t = fmtRate(bps); const i = t.indexOf(' ');
  return i < 0 ? [t, ''] : [t.slice(0, i), t.slice(i + 1)]; };

/** Heat band for a temperature readout. Colour is reinforcement; the number
 *  is always right next to it. */
function heat(c, warn, hot) {
  if (c === null || c === undefined) return '';
  if (c >= hot) return 'hot';
  if (c >= warn) return 'warn';
  return '';
}

function push(arr, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return;
  arr.push(Number(v));
  if (arr.length > HIST) arr.shift();
}

/* ── small render primitives ──────────────────────────────────────────── */

/* The console's readout tile: `.panel.corners` + `<i class="c">` for the
 * bracket marks, then `.stat` > `.label` / `.value` / `.meta`. This module
 * had its own `.lq-stat` / `.lq-k` / `.lq-v` / `.lq-sub` set that looked
 * close but tracked nothing, so LOQ's numbers and Home's never quite
 * matched. Same classes as home and agent now. */
function stat(id, k, v, unit = '', sub = '') {
  return `<div class="panel corners lq-stat" id="${id}"><i class="c"></i>
    <div class="stat">
      <span class="label">${esc(k)}</span>
      <span class="value"><span data-v>${esc(v)}</span>${
        unit ? `<sup style="font-size:0.9rem">${esc(unit)}</sup>` : ''}</span>
      ${sub ? `<span class="meta" data-sub>${esc(sub)}</span>` : ''}
    </div>
  </div>`;
}

function setStat(id, v, sub, band) {
  const el = $(`#${id}`);
  if (!el) return;
  const vEl = el.querySelector('[data-v]');
  if (vEl && vEl.textContent !== String(v)) vEl.textContent = v;
  if (sub !== undefined) {
    const s = el.querySelector('[data-sub]');
    if (s && s.textContent !== String(sub)) s.textContent = sub;
  }
  if (band !== undefined) {
    // `.is-warn` / `.is-err`, the design system's spelling. This module
    // used data-heat="warn|hot", which nothing else in the console
    // understood — and which had no CSS at all until recently.
    el.classList.remove('is-warn', 'is-err');
    if (band) el.classList.add(band === 'hot' ? 'is-err' : 'is-warn');
  }
}

/* Writes both halves of a rate readout: the number into [data-v] and the
   unit into the tile's <sup>. */
function setRate(id, bps) {
  const [n, u] = splitRate(bps);
  setStat(id, n);
  const sup = $(`#${id}`)?.querySelector('sup');
  if (sup && sup.textContent !== u) sup.textContent = u;
}

function sparkline(id, label, unit) {
  return `<div class="panel chartcard">
    <span class="label">${esc(label)}</span>
    <span class="value"><span id="${id}-now">—</span><sup style="font-size:0.9rem">${esc(unit)}</sup></span>
    <svg class="lq-spark" id="${id}" viewBox="0 0 300 46" preserveAspectRatio="none"
         role="img" aria-label="${esc(label)} over the last 90 seconds"></svg>
  </div>`;
}

/** Draws into a fixed 300x46 viewBox — the SVG scales, the geometry does not,
 *  so the path never needs recomputing on resize. */
function drawSpark(id, data, { min = 0, max = 100, stroke = 'var(--accent)' } = {}) {
  const svg = $(`#${id}`);
  if (!svg) return;
  if (data.length < 2) { svg.innerHTML = ''; return; }
  const lo = Math.min(min, ...data);
  const hi = Math.max(max, ...data);
  const span = Math.max(1e-6, hi - lo);
  const step = 300 / Math.max(1, HIST - 1);
  const pts = data.map((v, i) => {
    const x = (i + (HIST - data.length)) * step;
    const y = 44 - ((v - lo) / span) * 42;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const last = pts[pts.length - 1].split(',');
  svg.innerHTML =
    `<polyline points="${pts.join(' ')}" fill="none" stroke="${stroke}" stroke-width="1.5"
       vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
     <circle cx="${last[0]}" cy="${last[1]}" r="2" fill="${stroke}"/>`;
}

function viewMonitor() {
  return `
  <div class="stack-lg">
    <div class="section-head"><span class="idx">A / MONITOR</span><h2 class="h2">Monitor</h2></div>
    <div class="section-head"><span class="idx">A.1</span><h3 class="h2">CPU</h3><span class="spacer"></span>
      <span class="meta" id="lq-cpu-model">—</span></div>
    <div class="tiles">
      ${stat('lq-cpu-usage', 'load', '—', '%')}
      ${stat('lq-cpu-temp', 'temp', '—', '°C')}
      ${stat('lq-cpu-w', 'package', '—', 'W', 'of — W limit')}
      ${stat('lq-cpu-thr', 'throttle events', '—', '')}
    </div>
    <div class="lq-cores" id="lq-cores" role="img" aria-label="per-core load"></div>

    <div class="section-head"><span class="idx">A.2</span><h3 class="h2">GPU</h3><span class="spacer"></span>
      <span class="meta" id="lq-gpu-model">—</span></div>
    <div class="tiles">
      ${stat('lq-gpu-usage', 'load', '—', '%')}
      ${stat('lq-gpu-temp', 'temp', '—', '°C')}
      ${stat('lq-gpu-w', 'draw', '—', 'W')}
      ${stat('lq-gpu-clk', 'core', '—', 'MHz', 'mem — MHz')}
      ${stat('lq-gpu-vram', 'vram', '—', '', '— of —')}
    </div>

    <div class="grid grid--2">
      ${sparkline('lq-sp-cput', 'cpu temperature', '°C')}
      ${sparkline('lq-sp-gput', 'gpu temperature', '°C')}
      ${sparkline('lq-sp-cpuu', 'cpu load', '%')}
      ${sparkline('lq-sp-gpuu', 'gpu load', '%')}
    </div>

    <div class="section-head"><span class="idx">A.3</span><h3 class="h2">Cooling &amp; I/O</h3><span class="spacer"></span></div>
    <div class="tiles">
      ${stat('lq-fan-cpu', 'cpu fan', '—', 'rpm')}
      ${stat('lq-fan-gpu', 'gpu fan', '—', 'rpm')}
      ${stat('lq-net-rx', 'net down', '—', 'B/s')}
      ${stat('lq-net-tx', 'net up', '—', 'B/s')}
      ${stat('lq-dsk-r', 'disk read', '—', 'B/s')}
      ${stat('lq-dsk-w', 'disk write', '—', 'B/s')}
    </div>
  </div>`;
}

function viewBattery() {
  const h = snap?.battery?.history;
  return `<div class="stack-lg">
    <div class="section-head"><span class="idx">B / BATTERY</span><h2 class="h2">Battery</h2></div>
    <div class="tiles">
      ${stat('lq-bat-pct', 'charge', '—', '%')}
      ${stat('lq-bat-status', 'status', '—')}
      ${stat('lq-bat-w', 'flow', '—', 'W')}
      ${stat('lq-bat-health', 'health', '—', '%')}
      ${stat('lq-bat-cycles', 'cycles', '—')}
    </div>
    <div class="listrow">
      <span class="listrow-name">Conservation mode</span>
      <span class="meta">caps charging at ~80% to extend pack life</span>
      <span class="listrow-v">${snap?.state?.conservation ? 'ON' : 'OFF'}</span>
    </div>
    ${h ? `<div class="section-head"><span class="idx">B.1</span><h3 class="h2">Health history</h3><span class="spacer"></span></div>
    <div class="panel">
      <div class="listrow"><span class="listrow-name">tracking since</span>
        <span class="listrow-v">${esc(h.since || '—')}</span></div>
      <div class="listrow"><span class="listrow-name">first reading</span>
        <span class="listrow-v">${esc(h.first_h ?? '—')}%</span></div>
      <div class="listrow"><span class="listrow-name">latest reading</span>
        <span class="listrow-v">${esc(h.cur_h ?? '—')}%</span></div>
      <div class="listrow"><span class="listrow-name">samples</span>
        <span class="listrow-v">${esc(h.n ?? 0)}</span></div>
    </div>` : ''}
  </div>`;
}

function paint() {
  if (!snap || !root) return;

  if (view === 'monitor') {
    const c = snap.cpu || {}; const g = snap.gpu || {}; const f = snap.fans || {}; const io = snap.io || {};
    const m = $('#lq-cpu-model'); if (m) m.textContent = c.model || '';
    const gm = $('#lq-gpu-model'); if (gm) gm.textContent = g.model || '';

    setStat('lq-cpu-usage', num(c.usage, 1));
    setStat('lq-cpu-temp', num(c.tempC, 0), undefined, heat(c.tempC, 85, 95));
    setStat('lq-cpu-w', num(c.watts, 1), `of ${num(c.tdp)} W limit`);
    setStat('lq-cpu-thr', c.throttled === null ? '—' : Number(c.throttled).toLocaleString());

    setStat('lq-gpu-usage', num(g.usage, 0));
    setStat('lq-gpu-temp', num(g.tempC, 0), undefined, heat(g.tempC, 80, 88));
    setStat('lq-gpu-w', num(g.watts, 1));
    setStat('lq-gpu-clk', num(g.clockMhz, 0), `mem ${num(g.memClockMhz, 0)} MHz`);
    setStat('lq-gpu-vram', `${Math.round((g.vramUsedMb / (g.vramTotalMb || 1)) * 100)}%`,
      `${num(g.vramUsedMb, 0)} of ${num(g.vramTotalMb, 0)} MB`);

    setStat('lq-fan-cpu', f.cpuRpm ?? '—');
    setStat('lq-fan-gpu', f.gpuRpm ?? '—');
    setRate('lq-net-rx', io.netRxPerS);
    setRate('lq-net-tx', io.netTxPerS);
    setRate('lq-dsk-r', io.diskReadPerS);
    setRate('lq-dsk-w', io.diskWritePerS);

    const grid = $('#lq-cores');
    if (grid) {
      const cores = c.cores || [];
      if (grid.children.length !== cores.length) {
        grid.innerHTML = cores.map((_, i) =>
          `<div class="lq-cell" title="core ${i}"><span></span></div>`).join('');
      }
      cores.forEach((v, i) => {
        const cell = grid.children[i];
        if (!cell) return;
        const band = v >= 75 ? 3 : v >= 45 ? 2 : v >= 15 ? 1 : 0;
        if (cell.getAttribute('data-load') !== String(band)) cell.setAttribute('data-load', String(band));
        const t = String(Math.round(v));
        if (cell.firstChild.textContent !== t) cell.firstChild.textContent = t;
      });
    }

    drawSpark('lq-sp-cput', hist.cpuTemp, { min: 30, max: 100, stroke: 'var(--warn)' });
    drawSpark('lq-sp-gput', hist.gpuTemp, { min: 30, max: 95, stroke: 'var(--chart-3)' });
    drawSpark('lq-sp-cpuu', hist.cpuUsage, { min: 0, max: 100 });
    drawSpark('lq-sp-gpuu', hist.gpuUsage, { min: 0, max: 100, stroke: 'var(--chart-2)' });
    const setNow = (id, v, d) => { const e = $(id); if (e) e.textContent = num(v, d); };
    setNow('#lq-sp-cput-now', snap.cpu?.tempC, 0);
    setNow('#lq-sp-gput-now', snap.gpu?.tempC, 0);
    setNow('#lq-sp-cpuu-now', snap.cpu?.usage, 1);
    setNow('#lq-sp-gpuu-now', snap.gpu?.usage, 0);
  }

  if (view === 'battery') {
    const b = snap.battery || {};
    setStat('lq-bat-pct', b.percent ?? '—', undefined,
      b.percent !== null && b.percent <= 15 ? 'hot' : '');
    setStat('lq-bat-status', b.status || '—');
    setStat('lq-bat-w', num(b.powerW, 1));
    setStat('lq-bat-health', b.healthPct ?? '—');
    setStat('lq-bat-cycles', b.cycles ?? '—');
  }

}


function renderView() {
  const body = $('#lq-body');
  if (!body) return;
  body.innerHTML = view === 'battery' ? viewBattery() : viewMonitor();
  paint();
}

/* ── module contract ──────────────────────────────────────────────────── */

export default {
  async mount(mountEl, context) {
    root = mountEl;
    ctx = context;
    // The host mounts us AT a view — a deep link or a reload on #/loq/battery
    // arrives here, not through setView.
    view = context.view || 'monitor';

    if (!document.getElementById('lq-css')) {
      const link = document.createElement('link');
      link.id = 'lq-css';
      link.rel = 'stylesheet';
      link.href = `${ctx.base}/ui/loq.css`;
      document.head.appendChild(link);
    }

    mountEl.innerHTML = `<div class="lq"><div id="lq-body">
      <div class="skeleton" style="height:220px"></div>
    </div></div>`;

    try {
      snap = await ctx.api('/state');
      caps = snap.controls || {};
    } catch (e) {
      mountEl.innerHTML = `<div class="alert alert--err">
        <b>The LOQ agent is not answering.</b> ${esc(e.message)}</div>`;
      return;
    }

    renderView();

    ctx.sse('/events', {
      onMessage: (data) => {
        snap = data;
        caps = data.controls || caps;
        push(hist.cpuTemp, data.cpu?.tempC);
        push(hist.gpuTemp, data.gpu?.tempC);
        push(hist.cpuUsage, data.cpu?.usage);
        push(hist.gpuUsage, data.gpu?.usage);
        push(hist.cpuW, data.cpu?.watts);
        push(hist.gpuW, data.gpu?.watts);
        paint();
      },
    });
  },

  async setView(next) {
    if (next && next !== view) {
      view = next;
      renderView();
    }
  },

  async unmount() {
    snap = null;
    caps = {};
    for (const k of Object.keys(hist)) hist[k].length = 0;
    view = 'monitor';
    root = null;
    ctx = null;
  },
};
