/* ============================================================
   ojee-loq — module UI.

   Four views over one telemetry stream, matching the desktop
   app's tabs exactly so the two surfaces are the same mental
   model: Monitor / Power / Battery / System.

   Design points that are not obvious:

   * The skeleton is built ONCE per view and then repainted by
     id. Re-rendering innerHTML on every SSE tick would blow away
     the search box's focus mid-word and cancel a slider the user
     is still dragging — the readouts tick once a second.

   * Sliders commit on RELEASE ('change'), never while dragging
     ('input'). Dragging a TDP slider commits ~40 intermediate
     values otherwise, each one a real RAPL write.

   * Every advanced change arms the agent's auto-revert. The
     banner counts down locally between ticks so it reads as a
     deadline rather than a stuck number; the authority is always
     the agent's own `revertsInMs`.

   * Controls render only if the AGENT says the hardware exists.
     A LOQ without the ideapad module has no conservation_mode,
     and a switch that silently does nothing is worse than an
     absent one.
   ============================================================ */

let ctx = null;
let root = null;
let view = 'monitor';

let snap = null;
let caps = {};
let pending = null;
let pendingAt = 0;            // performance.now() when `pending` last arrived
let ticker = null;

let procTimer = null;
let procQuery = '';
let procData = { processes: [], total: 0, matched: 0 };

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

function stat(id, k, v, unit = '', sub = '') {
  return `<div class="lq-stat" id="${id}">
    <span class="lq-k">${esc(k)}</span>
    <span class="lq-v"><span data-v>${esc(v)}</span>${unit ? `<span class="lq-u"> ${esc(unit)}</span>` : ''}</span>
    ${sub ? `<span class="lq-sub" data-sub>${esc(sub)}</span>` : ''}
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
    if (band) el.setAttribute('data-heat', band);
    else el.removeAttribute('data-heat');
  }
}

function sparkline(id, label, unit) {
  return `<div class="lq-sparkwrap">
    <div class="lq-sparkhead">
      <span class="label">${esc(label)}</span>
      <span class="lq-v" id="${id}-now">—</span>
      <span class="lq-u">${esc(unit)}</span>
    </div>
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

/* ── control rows ─────────────────────────────────────────────────────── */

function ctlRow({ key, name, why, control, advanced }) {
  return `<div class="lq-ctl" data-adv="${advanced ? 1 : 0}" data-key="${key}">
    <div class="lq-ctl-txt">
      <span class="lq-ctl-name">${esc(name)}</span>
      <span class="lq-ctl-why">${esc(why)}</span>
    </div>
    <div class="lq-ctl-act">${control}</div>
  </div>`;
}

function seg(key, options, current) {
  return `<div class="segctl" role="group" aria-label="${esc(key)}">${options.map((o) => `
    <button type="button" data-ctl="${key}" data-value="${esc(o.value)}"
            aria-pressed="${String(o.value) === String(current)}">${esc(o.label)}</button>`).join('')}</div>`;
}

function toggle(key, on, labelText) {
  return `<label class="toggle">
    <input type="checkbox" data-ctl="${key}" ${on ? 'checked' : ''}
           aria-label="${esc(labelText)}">
    <span class="track" aria-hidden="true"></span>
  </label>`;
}

function slider(key, { min, max, step, value, unit, allowAuto, park = 'min' }) {
  const v = value === null || value === undefined
    ? (park === 'max' ? max : min) : value;
  return `<div class="lq-slider" data-slider="${key}">
    <input type="range" class="range" min="${min}" max="${max}" step="${step}" value="${v}"
           data-ctl-range="${key}" aria-label="${esc(key)}">
    <span class="lq-live" data-live>${value === null || value === undefined
      ? 'auto' : `${v}${unit ? esc(unit) : ''}`}</span>
    ${allowAuto ? `<button type="button" class="btn btn--sm btn--ghost" data-ctl-auto="${key}">AUTO</button>` : ''}
  </div>`;
}

/* ── commit path ──────────────────────────────────────────────────────── */

async function commit(key, value, { confirmText } = {}) {
  const advanced = !!ADVANCED[key];

  if (confirmText) {
    const ok = await ctx.modal({
      title: `Apply ${label(key)}?`,
      body: `<p class="meta">${confirmText}</p>${advanced ? `
        <p class="meta" style="margin-top:10px">This is an advanced control. It will be
        <b>reverted automatically</b> unless you confirm it afterwards — so a dropped
        connection cannot leave the machine mis-configured.</p>` : ''}`,
      actions: [
        { label: 'Cancel', value: false, variant: 'ghost' },
        { label: 'Apply', value: true },
      ],
    });
    if (!ok) { paint(); return false; }   // repaint snaps the slider back
  }

  try {
    const res = await ctx.api('/control', {
      method: 'POST',
      body: JSON.stringify({ key, value }),
    });
    pending = res.pending || null;
    pendingAt = performance.now();
    renderPending();
    ctx.toast('ok', label(key), `set to ${value === null ? 'auto' : value}`);
    return true;
  } catch (e) {
    ctx.toast('err', `${label(key)} failed`, e.message);
    paint();
    return false;
  }
}

const ADVANCED = { cpuTdp: 1, gpuClock: 1, gpuMemClock: 1, gpuTgp: 1, gpuMode: 1 };

// The banner and the confirm dialog are the two places a control is named
// without its row next to it, so they need the human name rather than the
// wire key — "CPUTDP" is not what the row above it is called.
const LABELS = {
  profile: 'Platform profile',
  cpuTdp: 'CPU power limit',
  gpuClock: 'GPU core clock',
  gpuMemClock: 'GPU memory clock',
  gpuTgp: 'GPU power (TGP)',
  gpuMode: 'GPU mode',
  conservation: 'Conservation mode',
  fnLock: 'Fn lock',
  kbdBacklight: 'Keyboard backlight',
  micMuted: 'Microphone',
  touchpad: 'Touchpad',
};
const label = (k) => LABELS[k] || k;

/* ── auto-revert banner ───────────────────────────────────────────────── */

function renderPending() {
  const host = $('#lq-pending-host');
  if (!host) return;
  if (!pending) { host.innerHTML = ''; return; }

  if (!host.querySelector('.lq-pending')) {
    host.innerHTML = `<div class="lq-pending" role="alert">
      <span class="lq-count" id="lq-count">—</span>
      <div class="lq-pending-txt">
        <div class="lq-ctl-name" id="lq-pending-key"></div>
        <div class="lq-ctl-why">reverts automatically unless you keep it</div>
      </div>
      <div class="lq-acts">
        <button type="button" class="btn btn--sm btn--ghost" id="lq-revert">REVERT NOW</button>
        <button type="button" class="btn btn--sm" id="lq-keep">KEEP</button>
      </div>
    </div>`;
    $('#lq-keep').addEventListener('click', async () => {
      await ctx.api('/revert', { method: 'POST', body: JSON.stringify({ confirm: true }) });
      pending = null; renderPending();
      ctx.toast('ok', 'kept', 'the change will not be reverted');
    });
    $('#lq-revert').addEventListener('click', async () => {
      await ctx.api('/revert', { method: 'POST', body: JSON.stringify({}) });
      pending = null; renderPending();
      ctx.toast('info', 'reverted', 'restored the previous value');
    });
  }
  $('#lq-pending-key').textContent = label(pending.key);
  tickPending();
}

function tickPending() {
  const el = $('#lq-count');
  if (!el || !pending) return;
  // Interpolated between ticks so it counts down smoothly; the agent's value
  // is still the authority and overwrites this every second.
  const left = Math.max(0, pending.revertsInMs - (performance.now() - pendingAt));
  el.textContent = `${Math.ceil(left / 1000)}s`;
}

/* ── views ────────────────────────────────────────────────────────────── */

function viewMonitor() {
  return `
  <div class="stack">
    <div class="section-head"><span class="label label--accent">CPU</span>
      <span class="meta" id="lq-cpu-model">—</span></div>
    <div class="lq-stats">
      ${stat('lq-cpu-usage', 'load', '—', '%')}
      ${stat('lq-cpu-temp', 'temp', '—', '°C')}
      ${stat('lq-cpu-w', 'package', '—', 'W', 'of — W limit')}
      ${stat('lq-cpu-thr', 'throttle events', '—', '')}
    </div>
    <div class="lq-cores" id="lq-cores" role="img" aria-label="per-core load"></div>

    <div class="section-head"><span class="label label--accent">GPU</span>
      <span class="meta" id="lq-gpu-model">—</span></div>
    <div class="lq-stats">
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

    <div class="section-head"><span class="label label--accent">COOLING &amp; I/O</span></div>
    <div class="lq-stats">
      ${stat('lq-fan-cpu', 'cpu fan', '—', 'rpm')}
      ${stat('lq-fan-gpu', 'gpu fan', '—', 'rpm')}
      ${stat('lq-net-rx', 'net down', '—')}
      ${stat('lq-net-tx', 'net up', '—')}
      ${stat('lq-dsk-r', 'disk read', '—')}
      ${stat('lq-dsk-w', 'disk write', '—')}
    </div>
  </div>`;
}

function viewPower() {
  const s = snap?.state || {};
  const g = snap?.gpu || {};
  const rows = [];

  if (caps.profile) {
    rows.push(ctlRow({
      key: 'profile', name: 'Platform profile',
      why: 'fan curve and power envelope, applied instantly',
      control: seg('profile', [
        { value: 'quiet', label: 'QUIET' },
        { value: 'balanced', label: 'BALANCED' },
        { value: 'balanced-performance', label: 'BAL-PERF' },
        { value: 'performance', label: 'PERF' },
      ], s.profile),
    }));
  }

  if (caps.cpuTdp) {
    rows.push(ctlRow({
      key: 'cpuTdp', name: 'CPU power limit', advanced: true,
      why: 'Intel RAPL long-term limit — commits when you release the slider',
      control: slider('cpuTdp', {
        min: 10, max: snap?.cpu?.tdpMax || 200, step: 5,
        value: snap?.cpu?.tdp, unit: 'W',
      }),
    }));
  }

  if (caps.gpuClock) {
    rows.push(ctlRow({
      key: 'gpuClock', name: 'GPU core clock', advanced: true,
      why: 'locks the core clock; AUTO releases it back to the driver',
      control: slider('gpuClock', {
        min: 180, max: 3090, step: 15, value: null, unit: 'MHz', allowAuto: true,
      }),
    }));
  }

  if (caps.gpuMemClock) {
    rows.push(ctlRow({
      key: 'gpuMemClock', name: 'GPU memory clock', advanced: true,
      why: 'locks the memory clock; AUTO releases it',
      control: slider('gpuMemClock', {
        min: 405, max: Math.round(g.memClockMhz || 9001), step: 50, value: null,
        unit: 'MHz', allowAuto: true,
      }),
    }));
  }

  if (caps.gpuTgp) {
    rows.push(ctlRow({
      key: 'gpuTgp', name: 'GPU power (TGP)', advanced: true,
      why: 'routed through the Lenovo EC — write-only, so the current value cannot be read back',
      control: slider('gpuTgp', { min: 45, max: 100, step: 5, value: null, unit: 'W', park: 'max' }),
    }));
  }

  if (caps.gpuMode) {
    rows.push(ctlRow({
      key: 'gpuMode', name: 'GPU mode', advanced: true,
      why: 'takes effect after a REBOOT — nothing changes until then',
      control: seg('gpuMode', [
        { value: 'hybrid', label: 'HYBRID' },
        { value: 'intel', label: 'INTEL' },
        { value: 'nvidia', label: 'NVIDIA' },
      ], s.gpuMode),
    }));
  }

  const locked = !caps.advancedEnabled ? `
    <div class="lq-off">
      <span class="dot dot--warn" aria-hidden="true"></span>
      <span class="lq-off-txt">Advanced controls are <b>disabled</b> on this agent.
      Set <code class="kbd">LOQ_ALLOW_ADVANCED=1</code> and restart it to permit CPU/GPU
      power and clock changes. Everything else on this page works regardless.</span>
    </div>` : '';

  return `<div class="stack">
    ${locked}
    <div class="lq-list">${rows.join('')}</div>
  </div>`;
}

function viewBattery() {
  const rows = [];
  if (caps.conservation) {
    rows.push(ctlRow({
      key: 'conservation', name: 'Conservation mode',
      why: 'caps charging at ~80% — the single biggest lever on pack lifespan',
      control: toggle('conservation', !!snap?.state?.conservation, 'conservation mode'),
    }));
  }
  const h = snap?.battery?.history;
  return `<div class="stack">
    <div class="lq-stats">
      ${stat('lq-bat-pct', 'charge', '—', '%')}
      ${stat('lq-bat-status', 'status', '—')}
      ${stat('lq-bat-w', 'flow', '—', 'W')}
      ${stat('lq-bat-health', 'health', '—', '%')}
      ${stat('lq-bat-cycles', 'cycles', '—')}
    </div>
    <div class="lq-list">${rows.join('')}</div>
    ${h ? `<div class="section-head"><span class="label label--accent">HEALTH HISTORY</span></div>
    <div class="lq-list">
      <div class="lq-row"><span class="lq-row-k">tracking since</span>
        <span class="lq-row-v">${esc(h.since || '—')}</span></div>
      <div class="lq-row"><span class="lq-row-k">first reading</span>
        <span class="lq-row-v">${esc(h.first_h ?? '—')}%</span></div>
      <div class="lq-row"><span class="lq-row-k">latest reading</span>
        <span class="lq-row-v">${esc(h.cur_h ?? '—')}%</span></div>
      <div class="lq-row"><span class="lq-row-k">samples</span>
        <span class="lq-row-v">${esc(h.n ?? 0)}</span></div>
    </div>` : ''}
  </div>`;
}

function viewSystem() {
  const s = snap?.state || {};
  const rows = [];
  if (caps.kbdBacklight) {
    rows.push(ctlRow({
      key: 'kbdBacklight', name: 'Keyboard backlight', why: 'off, low or high',
      control: seg('kbdBacklight', [
        { value: 0, label: 'OFF' }, { value: 1, label: 'LOW' }, { value: 2, label: 'HIGH' },
      ], s.kbdBacklight),
    }));
  }
  if (caps.fnLock) {
    rows.push(ctlRow({
      key: 'fnLock', name: 'Fn lock',
      why: 'when on, F1–F12 act as function keys without holding Fn',
      control: toggle('fnLock', !!s.fnLock, 'fn lock'),
    }));
  }
  if (caps.micMuted) {
    rows.push(ctlRow({
      key: 'micMuted', name: 'Microphone', why: 'mutes the default input source',
      control: toggle('micMuted', !!s.micMuted, 'microphone muted'),
    }));
  }
  if (caps.touchpad) {
    rows.push(ctlRow({
      key: 'touchpad', name: 'Touchpad', why: 'disable while an external mouse is in use',
      control: toggle('touchpad', s.touchpad !== false, 'touchpad enabled'),
    }));
  }

  const drives = (snap?.drives || []).map((d) => `
    <div class="lq-row">
      <span class="lq-row-k">${esc(d.dev)}</span>
      <span class="meta">${esc(d.model || '')}</span>
      <span class="lq-row-v">${fmtBytes(d.size)}</span>
    </div>`).join('') || '<div class="empty">no drives reported</div>';

  const nics = (snap?.nics || []).map((n) => `
    <div class="lq-row">
      <span class="lq-row-k">${esc(n.iface)}</span>
      <span class="meta">${esc(n.kind)}${n.ssid ? ` · ${esc(n.ssid)}` : ''}</span>
      <span class="lq-row-v">${esc(n.ip || '—')}</span>
    </div>`).join('') || '<div class="empty">no active interfaces</div>';

  return `<div class="stack">
    <div class="lq-list">${rows.join('')}</div>

    <div class="section-head"><span class="label label--accent">PROCESSES</span></div>
    <div class="lq-proc-bar">
      <input type="search" class="input" id="lq-proc-q" placeholder="Filter by name, command or pid"
             aria-label="filter processes" value="${esc(procQuery)}">
      <span class="lq-proc-count" id="lq-proc-count">—</span>
    </div>
    <div class="lq-proc-wrap">
      <table class="lq-proc">
        <thead><tr>
          <th>process</th><th class="lq-c-user">user</th>
          <th class="lq-r">cpu</th><th class="lq-r">memory</th>
          <th>state</th><th></th>
        </tr></thead>
        <tbody id="lq-proc-body"></tbody>
      </table>
    </div>

    <div class="section-head"><span class="label label--accent">STORAGE</span></div>
    <div class="lq-list">${drives}</div>
    <div class="section-head"><span class="label label--accent">NETWORK</span></div>
    <div class="lq-list">${nics}</div>
  </div>`;
}

/* ── process table ────────────────────────────────────────────────────── */

async function refreshProcs() {
  if (view !== 'system') return;
  try {
    procData = await ctx.api(`/processes?limit=60&q=${encodeURIComponent(procQuery)}`);
  } catch { return; }        // a dropped poll is not worth a toast
  paintProcs();
}

function paintProcs() {
  const body = $('#lq-proc-body');
  if (!body) return;
  const count = $('#lq-proc-count');
  if (count) {
    count.textContent = procQuery
      ? `${procData.matched} of ${procData.total} processes`
      : `${procData.total} processes`;
  }
  if (!procData.processes.length) {
    body.innerHTML = '<tr><td colspan="6"><div class="empty">nothing matches that filter</div></td></tr>';
    return;
  }
  body.innerHTML = procData.processes.map((p) => `
    <tr data-own="${p.own ? 1 : 0}">
      <td><div class="lq-p-name">${esc(p.name)}</div><div class="lq-p-cmd">${esc(p.cmdline)}</div></td>
      <td class="lq-c-user">${esc(p.user)}</td>
      <td class="lq-r">${p.cpuPct.toFixed(1)}%</td>
      <td class="lq-r">${fmtBytes(p.memKb * 1024)}</td>
      <td>${esc(p.state)}</td>
      <td class="lq-r">${p.own
        ? `<button type="button" class="btn btn--sm btn--danger" data-kill="${p.pid}"
                   aria-label="end ${esc(p.name)} (pid ${p.pid})">END</button>`
        : `<span class="lq-p-lock" title="the agent runs as another user and does not escalate">not yours</span>`}</td>
    </tr>`).join('');
}

async function killProc(pid) {
  const p = procData.processes.find((x) => x.pid === Number(pid));
  const choice = await ctx.modal({
    title: `End ${p?.name || `pid ${pid}`}?`,
    body: `<p class="meta">pid <b>${pid}</b>${p ? ` · ${esc(p.user)} · ${esc(p.cmdline)}` : ''}</p>
      <p class="meta" style="margin-top:10px">TERM asks the process to exit and lets it save.
      KILL is immediate and unsaved work is lost.</p>`,
    actions: [
      { label: 'Cancel', value: null, variant: 'ghost' },
      { label: 'TERM', value: 'TERM' },
      { label: 'FORCE KILL', value: 'KILL', variant: 'danger' },
    ],
  });
  if (!choice) return;
  try {
    await ctx.api('/kill', { method: 'POST', body: JSON.stringify({ pid: Number(pid), signal: choice }) });
    ctx.toast('ok', `sent SIG${choice}`, `pid ${pid}`);
    setTimeout(refreshProcs, 400);
  } catch (e) {
    ctx.toast('err', 'could not end process', e.message);
  }
}

/* ── paint ────────────────────────────────────────────────────────────── */

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
    setStat('lq-net-rx', fmtRate(io.netRxPerS));
    setStat('lq-net-tx', fmtRate(io.netTxPerS));
    setStat('lq-dsk-r', fmtRate(io.diskReadPerS));
    setStat('lq-dsk-w', fmtRate(io.diskWritePerS));

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

  // Controls reflect the machine, not the last click — if something else
  // changed a profile (the desktop app, a Fn key), this page follows.
  syncControls();
}

function syncControls() {
  const s = snap?.state || {};
  root.querySelectorAll('[data-ctl]').forEach((el) => {
    const key = el.getAttribute('data-ctl');
    if (!(key in s)) return;
    if (el.type === 'checkbox') {
      const want = key === 'touchpad' ? s[key] !== false : !!s[key];
      if (el.checked !== want && document.activeElement !== el) el.checked = want;
    } else if (el.hasAttribute('data-value')) {
      const on = String(s[key]) === el.getAttribute('data-value');
      el.setAttribute('aria-pressed', String(on));
    }
  });
  const tdp = root.querySelector('[data-ctl-range="cpuTdp"]');
  if (tdp && snap?.cpu?.tdp != null && document.activeElement !== tdp) {
    const wrap = tdp.closest('.lq-slider');
    if (!wrap.classList.contains('is-dirty')) {
      tdp.value = snap.cpu.tdp;
      wrap.querySelector('[data-live]').textContent = `${snap.cpu.tdp}W`;
    }
  }
}

/* ── events ───────────────────────────────────────────────────────────── */

function wire() {
  root.addEventListener('click', (e) => {
    const kill = e.target.closest('[data-kill]');
    if (kill) { killProc(kill.getAttribute('data-kill')); return; }

    const auto = e.target.closest('[data-ctl-auto]');
    if (auto) {
      const key = auto.getAttribute('data-ctl-auto');
      commit(key, null, { confirmText: `Release ${label(key)} back to automatic driver control.` });
      return;
    }

    const step = e.target.closest('[data-ctl][data-value]');
    if (step) {
      const key = step.getAttribute('data-ctl');
      let value = step.getAttribute('data-value');
      if (key === 'kbdBacklight') value = Number(value);
      const confirmText = ADVANCED[key]
        ? `Set ${label(key)} to ${value}.${key === 'gpuMode' ? ' This needs a REBOOT before anything changes.' : ''}`
        : null;
      commit(key, value, confirmText ? { confirmText } : {});
    }
  });

  root.addEventListener('change', (e) => {
    const box = e.target.closest('input[type="checkbox"][data-ctl]');
    if (box) {
      commit(box.getAttribute('data-ctl'), box.checked);
      return;
    }
    // 'change' on a range fires on RELEASE. This is the commit point;
    // 'input' below only moves the label.
    const range = e.target.closest('[data-ctl-range]');
    if (range) {
      const key = range.getAttribute('data-ctl-range');
      const value = Number(range.value);
      const unit = key === 'cpuTdp' || key === 'gpuTgp' ? 'W' : 'MHz';
      commit(key, value, { confirmText: `Set ${label(key)} to ${value}${unit}.` })
        .then(() => range.closest('.lq-slider')?.classList.remove('is-dirty'));
    }
  });

  root.addEventListener('input', (e) => {
    const range = e.target.closest('[data-ctl-range]');
    if (range) {
      const wrap = range.closest('.lq-slider');
      const key = range.getAttribute('data-ctl-range');
      const unit = key === 'cpuTdp' || key === 'gpuTgp' ? 'W' : 'MHz';
      wrap.classList.add('is-dirty');
      wrap.querySelector('[data-live]').textContent = `${range.value}${unit}`;
      return;
    }
    const q = e.target.closest('#lq-proc-q');
    if (q) { procQuery = q.value.trim(); refreshProcs(); }
  });
}

function renderView() {
  const body = $('#lq-body');
  if (!body) return;
  body.innerHTML = view === 'monitor' ? viewMonitor()
    : view === 'power' ? viewPower()
    : view === 'battery' ? viewBattery()
    : viewSystem();
  paint();

  clearInterval(procTimer);
  if (view === 'system') {
    refreshProcs();
    procTimer = setInterval(refreshProcs, 2000);
  }
}

/* ── module contract ──────────────────────────────────────────────────── */

export default {
  async mount(mountEl, context) {
    root = mountEl;
    ctx = context;
    // The host mounts us AT a view — deep links and a reload on #/power both
    // arrive here, not through setView. Defaulting to 'monitor' regardless
    // silently redirected every bookmark to the first tab.
    view = context.view || 'monitor';

    if (!document.getElementById('lq-css')) {
      const link = document.createElement('link');
      link.id = 'lq-css';
      link.rel = 'stylesheet';
      link.href = `${ctx.base}/ui/loq.css`;
      document.head.appendChild(link);
    }

    mountEl.innerHTML = `<div class="lq">
      <div id="lq-pending-host"></div>
      <div id="lq-body"><div class="skeleton" style="height:220px"></div></div>
    </div>`;
    wire();

    try {
      snap = await ctx.api('/state');
      caps = snap.controls || {};
      pending = snap.pending || null;
      pendingAt = performance.now();
    } catch (e) {
      mountEl.innerHTML = `<div class="alert alert--err">
        <b>The LOQ agent is not answering.</b> ${esc(e.message)}</div>`;
      return;
    }

    renderView();
    renderPending();

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

        const had = !!pending;
        pending = data.pending || null;
        pendingAt = performance.now();
        if (had !== !!pending || pending) renderPending();
        paint();
      },
    });

    ticker = setInterval(tickPending, 250);
    ctx.onCleanup?.(() => clearInterval(ticker));
  },

  async setView(next) {
    if (next && next !== view) {
      view = next;
      renderView();
    }
  },

  async unmount() {
    clearInterval(ticker);
    clearInterval(procTimer);
    ticker = procTimer = null;
    snap = null; caps = {}; pending = null;
    procData = { processes: [], total: 0, matched: 0 };
    procQuery = '';
    view = 'monitor';
    for (const k of Object.keys(hist)) hist[k].length = 0;
    root = null; ctx = null;
  },
};
