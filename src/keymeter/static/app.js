'use strict';

// keymeter dashboard. Polls the local server (keymeter web) every 2s; the server polls the
// gateway on its own schedule. All values from the gateway go into the DOM via textContent.

const REFRESH_MS = 2000;
const RANGES = [['15m', 900], ['1h', 3600], ['6h', 21600], ['24h', 86400]];
const TIME_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
const THEMES = ['auto', 'light', 'dark'];
const STATUS = {
  OK: ['good', 'circle-check'],
  STARTING: ['neutral', 'loader'],
  OFFLINE: ['warning', 'wifi-off'],
  'RATE LIMITED': ['warning', 'triangle-alert'],
};
const EVENT_STYLE = { good: ['good', 'circle-check'], info: ['neutral', 'info'], warn: ['warning', 'triangle-alert'], crit: ['critical', 'octagon-x'] };
const FAVICON_COLOR = { good: '#0ca30c', warning: '#fab219', critical: '#d03b3b', neutral: '#2a78d6' };
const GATEWAY_NAME = { litellm: 'LiteLLM', openrouter: 'OpenRouter' };

const S = {
  boot: null, version: -1, data: null, history: [], offset: 0,
  serverOk: null, bellSeq: null, pollPending: false, bannerKey: null, favicon: null,
  status: null, lastEventTs: 0, meters: new Map(), themeTimer: 0,
  range: load('keymeter-range', '1h'), theme: load('keymeter-theme', 'auto'), alerts: load('keymeter-alerts', 'off') === 'on',
};
let charts, audio;

// ---------------------------------------------------------------- helpers

function load(k, def) { try { return localStorage.getItem(k) ?? def; } catch { return def; } }
function save(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } }

const $ = id => document.getElementById(id);
const SVG_NS = 'http://www.w3.org/2000/svg';

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k === 'style') Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  return add(el, kids);
}
function add(el, kids) {
  for (const k of [kids].flat(Infinity)) {
    if (k == null || k === false || k === '') continue;
    el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}
// replaceChildren() that also takes arrays and skips null/false, like h()
const fill = (el, ...kids) => { el.replaceChildren(); return add(el, kids); };
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) el.setAttribute(k, v);
  return add(el, kids);
}
function icon(name, cls = '') {
  return sv('svg', { class: `i ${cls}`.trim(), 'aria-hidden': 'true', focusable: 'false' }, sv('use', { href: `#i-${name}` }));
}
const dim = text => h('span', { class: 'dim', text });
const withIcon = (cls, ic, ...kids) => h('span', { class: `with-icon ${cls}` }, icon(ic), ...kids);
const nowS = () => Date.now() / 1000 + S.offset;  // server clock

// ---------------------------------------------------------------- fades
// Only real transitions fade (something appears, disappears or is switched by the user), never the
// redraw every poll triggers, so the page doesn't flicker. Nothing animates under reduced motion.

const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
const fadingOut = new WeakSet();

function fadeIn(el, ms = 220) {
  if (!reduceMotion.matches) el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: ms, easing: 'ease-out' });
}

// Show or hide `el` with a fade. A hidden element keeps its content until the fade-out finishes.
function setShown(el, show) {
  if (show ? !el.hidden && !fadingOut.has(el) : el.hidden || fadingOut.has(el)) return;
  el.getAnimations().forEach(a => a.cancel());
  fadingOut.delete(el);
  if (show) {
    el.hidden = false;
    fadeIn(el);
  } else if (reduceMotion.matches) {
    el.hidden = true;
  } else {
    fadingOut.add(el);
    el.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 180, easing: 'ease-in' }).finished
      .then(() => { if (fadingOut.delete(el)) el.hidden = true; }, () => { /* cancelled: shown again */ });
  }
}

// ---------------------------------------------------------------- formatting (mirrors keymeter/util.py)

function usd(x) {
  if (x == null) return '—';
  const a = Math.abs(x);
  if (a >= 100) return '$' + x.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (a >= 1) return '$' + x.toFixed(3);
  if (x && a < 0.01) return '$' + x.toFixed(5);
  return '$' + x.toFixed(4);
}

function fmtDur(sec) {
  if (sec == null) return '—';
  const sign = sec < 0 ? '-' : '';
  let s = Math.floor(Math.abs(sec));
  const d = Math.floor(s / 86400); s %= 86400;
  const hr = Math.floor(s / 3600); s %= 3600;
  const m = Math.floor(s / 60); s %= 60;
  const p2 = n => String(n).padStart(2, '0');
  if (d) return `${sign}${d}d ${hr}h`;
  if (hr) return `${sign}${hr}h ${p2(m)}m`;
  if (m) return `${sign}${m}m ${p2(s)}s`;
  return `${sign}${s}s`;
}

const fmtClock = ts => new Date(ts * 1000).toLocaleString('en-US', { weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
const fmtTime = (ts, secs) => new Date(ts * 1000).toLocaleTimeString('en-GB', secs
  ? { hour: '2-digit', minute: '2-digit', second: '2-digit' } : { hour: '2-digit', minute: '2-digit' });
const fmtLimit = v => (v == null || v === '' ? 'none' : typeof v === 'number' ? Math.trunc(v).toLocaleString('en-US') : String(v));
const fmtMin = m => `${+m.toFixed(2)}m`;

// A duration that keeps moving between polls: `base` seconds as of the current summary, counting down or up.
function tick(base, dir = 'down') {
  if (base == null) return document.createTextNode('—');
  const el = h('span', { class: 'tick-val', 'data-base': base, 'data-at': S.data.summary_time, 'data-dir': dir });
  el.textContent = fmtDur(tickValue(el));
  return el;
}
function tickValue(el) {
  const age = nowS() - +el.dataset.at, base = +el.dataset.base;
  return el.dataset.dir === 'up' ? base + age : Math.max(base - age, 0);
}

function level(pct) {
  const c = S.data.config;
  if (pct == null) return 'ok';
  return pct >= c.crit * 100 ? 'crit' : pct >= c.warn * 100 ? 'warn' : 'ok';
}
const statusInfo = st => STATUS[st] || ['critical', 'octagon-x'];

// ---------------------------------------------------------------- data loop

async function refresh() {
  const last = S.history[S.history.length - 1];
  try {
    const r = await fetch(`/api/state?since=${last ? last[0] : 0}`, { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    S.offset = d.server_time - Date.now() / 1000;
    if (d.boot !== S.boot) {  // server restarted: its history starts over
      S.boot = d.boot; S.history = []; S.version = -1; S.bellSeq = null;
    }
    for (const p of d.history) S.history.push(p);
    const newest = S.history.length ? S.history[S.history.length - 1][0] : 0;
    const keepFrom = S.history.findIndex(p => p[0] >= newest - d.config.history_s);
    if (keepFrom > 0) S.history.splice(0, keepFrom);
    S.serverOk = true;
    S.data = d;
    if (d.version !== S.version) {
      S.version = d.version;
      S.pollPending = false;
      render();
      checkBell(d);
    }
  } catch {
    S.serverOk = false;
  }
  renderBanner();
  renderLive();
}

async function loop() {
  await refresh();
  setTimeout(loop, S.pollPending || S.data?.poller.polling ? 700 : REFRESH_MS);
}

async function pollNow() {
  S.pollPending = true;
  renderLive();
  try {
    const r = await fetch('/api/poll', { method: 'POST' });
    if (!(await r.json()).queued) S.pollPending = false;
  } catch {
    S.pollPending = false;
  }
  renderLive();
}

// ---------------------------------------------------------------- alerts

function checkBell(d) {
  if (S.bellSeq != null && d.bell_seq > S.bellSeq && S.alerts) {
    const ev = d.events[d.events.length - 1];
    if (ev) {
      beep(ev.level);
      try {
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification(`keymeter: ${d.summary.status}`, { body: ev.text, tag: 'keymeter' });
        }
      } catch { /* notifications need a service worker on some platforms */ }
    }
  }
  S.bellSeq = d.bell_seq;
}

function beep(lvl) {
  try {
    audio = audio || new AudioContext();
    audio.resume();
    const t = audio.currentTime, o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = lvl === 'good' ? 880 : lvl === 'crit' ? 440 : 660;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(0.2, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
    o.connect(g).connect(audio.destination);
    o.start(t);
    o.stop(t + 0.4);
  } catch { /* audio unavailable */ }
}

async function toggleAlerts() {
  S.alerts = !S.alerts;
  save('keymeter-alerts', S.alerts ? 'on' : 'off');
  if (S.alerts) {
    beep('good');  // inside the click, so the browser lets audio start
    if ('Notification' in window && Notification.permission === 'default') await Notification.requestPermission();
  }
  renderAlertsBtn();
  fadeIn($('alerts-btn').firstChild);
}

function renderAlertsBtn() {
  const btn = $('alerts-btn');
  const perm = 'Notification' in window ? Notification.permission : 'unsupported';
  const label = !S.alerts
    ? (window.isSecureContext
      ? 'Alerts off. Turn on for a desktop notification and a sound on status changes and budget thresholds'
      : 'Alerts off. Turn on for a sound on status changes and budget thresholds (desktop notifications need HTTPS)')
    : perm === 'granted' ? 'Alerts on: desktop notification and sound'
      : !window.isSecureContext ? 'Alerts on: sound only (browsers allow desktop notifications only over HTTPS or on localhost)'
        : 'Alerts on: sound only (notifications are blocked for this page in the browser)';
  btn.setAttribute('aria-pressed', String(S.alerts));
  btn.setAttribute('aria-label', label);
  btn.title = label;
  btn.replaceChildren(icon(S.alerts ? 'bell' : 'bell-off'));
}

// ---------------------------------------------------------------- theme

function applyTheme(animate = false) {
  const root = document.documentElement;
  if (animate && !reduceMotion.matches) {  // crossfade the colours (see .theming in styles.css)
    root.classList.add('theming');
    clearTimeout(S.themeTimer);
    S.themeTimer = setTimeout(() => root.classList.remove('theming'), 400);
  }
  if (S.theme === 'auto') delete root.dataset.theme; else root.dataset.theme = S.theme;
  const btn = $('theme-btn'), label = `Theme: ${S.theme}. Click to change`;
  btn.replaceChildren(icon({ auto: 'monitor', light: 'sun', dark: 'moon' }[S.theme]));
  btn.setAttribute('aria-label', label);
  btn.title = label;
  if (animate) fadeIn(btn.firstChild);
}

// ---------------------------------------------------------------- render: frame

function render() {
  const d = S.data, s = d.summary, has = !!s.key_info;
  renderHeader(s);
  // swap the empty state and the dashboard: the old one goes at once, the new one fades in
  for (const [el, show] of [[$('empty'), !has], [$('dash'), has]]) {
    if (el.hidden === show) { el.hidden = !show; if (show) fadeIn(el); }
  }
  if (has) {
    renderKpis(s);
    renderProjections(s);
    renderLimits(s);
    renderCharts();
    // gateways that report no per-model spend (OpenRouter) get no models card
    $('models-card').hidden = !s.features.includes('models');
    renderModels(s);
  } else {
    renderEmpty(s, d);
  }
  renderEvents(d.events);
  renderFoot(d);
  renderTitle(s);
}

function renderHeader(s) {
  const ki = s.key_info || {};
  let host = s.gateway;
  try { host = new URL(s.gateway).host; } catch { /* keep as is */ }
  const sep = () => h('span', { class: 'sep', 'aria-hidden': 'true', text: '·' });
  fill($('ident'),
    h('span', { title: s.gateway, text: host }), sep(),
    h('span', { text: `key ${s.key}${ki.alias ? ` (${ki.alias})` : ''}` }),
  );
  const [tone, ic] = statusInfo(s.status);
  const pill = h('span', { class: `pill st-${tone}` }, icon(ic, s.status === 'STARTING' ? 'spin' : ''), s.status);
  $('status-slot').replaceChildren(pill);
  if (s.status !== S.status) { S.status = s.status; fadeIn(pill); }
}

function renderBanner() {
  const d = S.data, s = d?.summary, rows = [];
  if (S.serverOk === false) {
    rows.push(['critical', 'unplug', 'Lost the connection to the keymeter server. Retrying every 2s; check that `keymeter web` is still running.']);
  }
  if (s && s.status !== 'OK' && s.status !== 'STARTING') {
    const [tone, ic] = statusInfo(s.status);
    rows.push([tone, ic, [h('strong', { text: s.status }), s.message ? ` · ${s.message}` : '']]);
  }
  if (d?.stale && s.data_age_s != null) {
    rows.push(['warning', 'clock', ['Gateway not answering. The numbers below are from ', tick(s.data_age_s, 'up'), ' ago.']]);
  }
  const keys = rows.map(r => JSON.stringify([r[0], r[1], typeof r[2] === 'string' ? r[2] : s?.message]));
  const key = JSON.stringify(keys);
  if (key === S.bannerKey) return;  // unchanged: don't re-announce
  const shown = new Set(S.bannerKey ? JSON.parse(S.bannerKey) : []);
  S.bannerKey = key;
  const el = $('banner');
  if (!rows.length) return setShown(el, false);  // the old rows stay while they fade out
  const els = rows.map(([tone, ic, msg]) => h('div', { class: `banner st-${tone}`, role: 'alert' }, icon(ic), h('div', { class: 'banner-msg' }, msg)));
  el.replaceChildren(...els);
  setShown(el, true);
  els.forEach((row, i) => { if (!shown.has(keys[i])) fadeIn(row); });
}

function renderLive() {
  const d = S.data;
  let state, text;
  if (S.serverOk === false) {
    state = 'offline'; text = 'Dashboard server offline';
  } else if (!d || d.poller.polling || d.poller.last_poll_at == null) {
    state = 'polling'; text = d ? 'Polling the gateway…' : 'Connecting…';
  } else {
    const p = d.poller, now = nowS();
    state = d.summary.status === 'OK' && !d.stale ? 'live' : 'warn';
    text = `Polled ${fmtDur(now - p.last_poll_at)} ago · ${p.latency_ms} ms`;
    if (p.next_poll_at) text += ` · next in ${fmtDur(Math.max(p.next_poll_at - now, 0))}`;
    if (p.fail_streak > 1) text += ' · backing off';
  }
  $('live').dataset.state = state;
  $('live-text').textContent = text;
  $('poll-now').disabled = S.serverOk !== true || !d || d.poller.polling || S.pollPending;
}

function renderTitle(s) {
  const pct = s.key_info?.used_pct;
  document.title = `${s.status === 'OK' && pct != null ? `${Math.round(pct)}% · ` : ''}${s.status} · keymeter`;
  let tone = statusInfo(s.status)[0];
  if (tone === 'good' && level(pct) !== 'ok') tone = level(pct) === 'crit' ? 'critical' : 'warning';
  const color = FAVICON_COLOR[tone], frac = pct == null ? 1 : Math.min(Math.max(pct, 0), 100) / 100;
  const key = `${color}${frac.toFixed(2)}`;
  if (key === S.favicon) return;
  S.favicon = key;
  const r = 11, c = 2 * Math.PI * r;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><circle cx="16" cy="16" r="${r}" fill="none" stroke="${color}" stroke-opacity=".28" stroke-width="7"/>`
    + `<circle cx="16" cy="16" r="${r}" fill="none" stroke="${color}" stroke-width="7" stroke-dasharray="${(frac * c).toFixed(2)} ${c.toFixed(2)}" transform="rotate(-90 16 16)"/></svg>`;
  $('favicon').href = 'data:image/svg+xml,' + encodeURIComponent(svg);
}

function renderEmpty(s, d) {
  const [tone, ic] = statusInfo(s.status);
  const next = d.poller.next_poll_at ? tick(d.poller.next_poll_at - d.summary_time) : null;
  const msg = s.status === 'STARTING'
    ? ['First poll in progress…']
    : [`The gateway answered ${s.status}, so there is no spend or budget data to show yet. Retrying`,
      next ? [' (next poll in ', next, ')'] : '', '; the dashboard fills in as soon as the gateway answers.'];
  $('empty').replaceChildren(
    h('div', { class: `empty-icon st-${tone}` }, icon(ic, s.status === 'STARTING' ? 'spin' : '')),
    h('div', {}, h('h2', { text: 'No quota data yet' }), h('p', {}, msg)),
  );
}

function renderEvents(events) {
  const list = $('events');
  if (!events.length) {
    list.replaceChildren(h('li', { class: 'table-empty', text: 'No events yet' }));
    return;
  }
  const fresh = [];
  list.replaceChildren(...events.slice().reverse().map(ev => {
    const [tone, ic] = EVENT_STYLE[ev.level] || EVENT_STYLE.info;
    const li = h('li', {},
      h('span', { class: 'ev-time', text: fmtTime(ev.ts, true) }),
      h('span', { class: `ev-icon st-${tone}` }, icon(ic)),
      h('span', { class: 'ev-text', text: ev.text }));
    if (ev.ts > S.lastEventTs) fresh.push(li);
    return li;
  }));
  S.lastEventTs = Math.max(S.lastEventTs, ...events.map(ev => ev.ts));
  fresh.forEach(li => fadeIn(li, 400));
}

function renderFoot(d) {
  const c = d.config, p = d.poller;
  $('foot').textContent = `Polls every ${c.interval}s · burn-rate window ${fmtMin(c.window)} · alerts at `
    + `${Math.round(c.warn * 100)}% / ${Math.round(c.crit * 100)}% · ${p.polls} polls, ${p.fails} failed · history kept in memory for 24h`;
}

// ---------------------------------------------------------------- render: KPI tiles

function meter(pct, label, small) {
  const c = S.data.config;
  const el = h('div', {
    class: `meter lv-${level(pct)}${small ? ' sm' : ''}`, role: 'meter', 'aria-label': label,
    'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': pct.toFixed(1), 'aria-valuetext': `${pct.toFixed(1)}% used`,
  });
  // the tile is rebuilt every poll, so start the fill where it was and let its CSS transition move it
  const w = Math.min(Math.max(pct, 0), 100), was = S.meters.get(label) ?? 0;
  const fillEl = h('div', { class: 'meter-fill', style: { width: `${was}%` } });
  el.append(fillEl);
  S.meters.set(label, w);
  if (was !== w) requestAnimationFrame(() => { void fillEl.offsetWidth; fillEl.style.width = `${w}%`; });
  if (!small) {
    for (const t of [c.warn, c.crit]) el.append(h('span', { class: 'meter-tick', style: { left: `${t * 100}%` }, title: `alert at ${Math.round(t * 100)}%` }));
  }
  return el;
}

function levelTag(pct) {
  const lv = level(pct);
  if (lv === 'ok') return null;
  return h('span', { class: `lvl t-${lv}` }, icon(lv === 'crit' ? 'octagon-x' : 'triangle-alert'), lv === 'crit' ? 'Critical' : 'Warning');
}

function budgetTile(v) {
  const label = text => h('div', { class: 'tile-label', text });
  const tile = h('section', { class: 'card tile tile-hero' });
  if (v.max_budget == null) {
    return add(tile, [label('Key spend'), h('div', { class: 'tile-value', text: usd(v.spend) }),
      h('p', { class: 'tile-sub', text: 'No budget cap on this key' })]);
  }
  return add(tile, [
    h('div', { class: 'tile-top' }, label('Key budget left'), levelTag(v.used_pct)),
    h('div', { class: 'tile-value', text: usd(v.remaining) }),
    meter(v.used_pct, 'Key budget used'),
    h('p', { class: 'tile-sub' }, h('strong', { text: `${v.used_pct.toFixed(1)}%` }), ` used · ${usd(v.spend)} of ${usd(v.max_budget)}`),
    v.reset_in_s != null && h('p', { class: 'tile-sub' }, 'Resets in ', tick(v.reset_in_s), v.budget_duration ? ` (every ${v.budget_duration})` : ''),
    v.soft_budget ? h('p', { class: 'tile-sub', text: `Soft limit ${usd(v.soft_budget)}` }) : null,
  ]);
}

function burnTile(v) {
  const r = v.burn_per_hour;
  return h('section', { class: 'card tile' },
    h('div', { class: 'tile-label', text: 'Burn rate' }),
    r == null ? h('div', { class: 'tile-value small dim', text: 'measuring…' })
      : h('div', { class: 'tile-value' }, usd(r), h('span', { class: 'unit', text: '/h' })),
    h('p', { class: 'tile-sub', text: `Over the last ${fmtMin(S.data.config.window)}` }));
}

// When the key budget runs out at the current burn rate.
function runoutTile(v) {
  const tile = h('section', { class: 'card tile' }, h('div', { class: 'tile-label', text: 'Runs out in' }));
  const value = (cls, ...kids) => h('div', { class: `tile-value ${cls}` }, ...kids);
  const sub = (...kids) => h('p', { class: 'tile-sub' }, ...kids);
  if (v.max_budget == null) return add(tile, [value('small dim', 'no cap'), sub('This key has no budget cap')]);
  if (v.burn_per_hour == null) return add(tile, [value('small dim', 'measuring…'), sub('Needs two polls to measure the burn rate')]);
  if (v.runs_out_in_s == null) return add(tile, [value('small t-good', 'not at this rate'), sub(`No spend in the last ${fmtMin(S.data.config.window)}`)]);
  const eta = v.runs_out_in_s;
  if (!eta) return add(tile, [value('t-crit', icon('octagon-x'), 'used up'), sub('No budget left until it resets')]);
  if (v.reset_in_s != null && eta > v.reset_in_s) {
    return add(tile, [value('t-good', tick(eta)), sub('But the budget resets first, in ', tick(v.reset_in_s))]);
  }
  const crit = eta < 3600;
  return add(tile, [
    value(crit ? 't-crit' : 't-warn', icon(crit ? 'octagon-x' : 'triangle-alert'), tick(eta)),
    sub(`Around ${fmtClock(S.data.summary_time + eta)}, before the budget resets`),
  ]);
}

function renderKpis(s) {
  const ki = s.key_info;
  $('kpis').replaceChildren(budgetTile(ki), burnTile(ki), runoutTile(ki));
}

// ---------------------------------------------------------------- render: tables

function table(head, rows) {
  return h('table', {},
    h('thead', {}, h('tr', {}, head.map(([label, cls]) => h('th', { class: cls, scope: 'col' }, label)))),
    h('tbody', {}, rows.map(cells => h('tr', {}, cells.map((c, i) => h('td', { class: head[i][1] }, c))))));
}

// Label / value rows.
function kvTable(rows) {
  return h('table', {}, h('tbody', {}, rows.map(([label, value]) =>
    h('tr', {}, h('th', { scope: 'row' }, label), h('td', { class: 'num' }, value)))));
}

function runoutCell(v) {
  if (v.max_budget == null) return dim('no cap');
  if (v.burn_per_hour == null) return dim('measuring…');
  if (v.runs_out_in_s == null) return h('span', { class: 't-good', text: 'not at this rate' });
  const eta = v.runs_out_in_s;
  if (!eta) return withIcon('t-crit', 'octagon-x', 'used up');
  if (v.reset_in_s != null && eta > v.reset_in_s) return h('span', { class: 't-good' }, tick(eta), ' (after reset)');
  const crit = eta < 3600;
  return withIcon(crit ? 't-crit' : 't-warn', crit ? 'octagon-x' : 'triangle-alert', tick(eta), ` · ${fmtClock(S.data.summary_time + eta)}`);
}

function projectedCell(v) {
  const p = v.projected_at_reset;
  if (p == null) return dim('—');
  if (v.max_budget != null && p > v.max_budget) return withIcon('t-crit', 'octagon-x', `${usd(p)} > budget`);
  return h('span', { class: 't-good', text: usd(p) });
}

function renderProjections(s) {
  const v = s.key_info;
  $('proj-sub').textContent = `Burn rate over the last ${fmtMin(S.data.config.window)}; projections assume it holds`;
  $('proj').replaceChildren(kvTable([
    ['Burn rate', v.burn_per_hour == null ? dim('measuring…') : `${usd(v.burn_per_hour)}/h`],
    ['This session', v.session_spend == null ? dim('—') : [`+${usd(v.session_spend)} in `, tick(s.session_s, 'up')]],
    ['Runs out in', runoutCell(v)],
    ['Projected at reset', projectedCell(v)],
    ['Budget resets in', v.reset_in_s == null ? dim('—') : [tick(v.reset_in_s), v.budget_duration ? dim(` · every ${v.budget_duration}`) : '']],
    ['Soft limit', v.soft_budget ? usd(v.soft_budget) : dim('none')],
  ]));
}

function renderLimits(s) {
  const v = s.key_info, exp = v.expires_in_s, full = s.features.includes('limits');
  const expires = exp == null ? 'never' : exp > 0
    ? h('span', { class: exp < 86400 ? 't-crit' : '' }, 'in ', tick(exp))
    : withIcon('t-crit', 'octagon-x', 'expired');
  $('limits').closest('.card').classList.toggle('fit', !full);
  $('limits-sub').textContent = full ? 'Rate limits, expiry and model access'
    : `Key expiry. ${GATEWAY_NAME[s.gateway_type] || s.gateway_type} doesn't report rate limits or model access`;
  fill($('limits'),
    h('div', { class: 'table-wrap' }, kvTable(full ? [
      ['RPM limit', fmtLimit(v.rpm_limit)],
      ['TPM limit', fmtLimit(v.tpm_limit)],
      ['Parallel requests', fmtLimit(v.max_parallel_requests)],
      ['Blocked', v.blocked ? withIcon('t-crit', 'octagon-x', 'yes') : dim('no')],
      ['Key expires', expires],
    ] : [['Key expires', expires]])),
    full && h('div', { class: 'chips' },
      h('span', { class: 'chips-label', text: 'Allowed models' }),
      v.models.length ? v.models.map(m => h('span', { class: 'chip', text: m })) : h('span', { class: 'chip', text: 'all models' })),
  );
}

function renderModels(s) {
  const rows = s.models;
  if (!rows.length) {
    $('models').replaceChildren(h('p', { class: 'table-empty', text: 'The gateway has not reported per-model spend for this key yet.' }));
    return;
  }
  const withLimits = rows.some(r => r.limit);
  const head = [['Model', ''], ['Spend', 'num'], ['This session', 'num'], ['Share', 'num'], ...(withLimits ? [['Model budget', 'num budget-col']] : [])];
  $('models').replaceChildren(table(head, rows.map(r => [
    h('span', { class: 'mono', text: r.model }),
    usd(r.spend),
    r.session_spend == null ? dim('—') : r.session_spend ? `+${usd(r.session_spend)}` : dim(`+${usd(0)}`),
    r.share_pct == null ? dim('—') : h('span', { class: 'share' },
      h('span', { class: 'share-track', 'aria-hidden': 'true' }, h('span', { class: 'share-fill', style: { width: `${r.share_pct}%` } })),
      `${Math.round(r.share_pct)}%`),
    ...(withLimits ? [r.limit ? h('span', { class: 'budget-cell' },
      meter(r.used_pct, `${r.model} budget used`, true),
      h('span', { text: `${usd(r.period_spend)} / ${usd(r.limit)}` }),
      (r.period || level(r.used_pct) !== 'ok') && h('span', { class: 'dim' }, r.period ? `per ${r.period} ` : '', levelTag(r.used_pct)))
      : dim('—')] : []),
  ])));
}

// ---------------------------------------------------------------- charts

function niceTicks(lo, hi, count) {
  if (!(hi > lo)) {
    const pad = Math.abs(hi) * 0.02 || 0.01;
    lo = lo >= 0 ? Math.max(0, lo - pad) : lo - pad;
    hi += pad;
  }
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw)), norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let i = 0; start + i * step <= end + step / 2; i++) ticks.push(+(start + i * step).toPrecision(12) + 0);
  return { ticks, step, lo: start, hi: end };
}

function fmtTick(v, step) {
  const dec = step >= 1 ? 0 : Math.max(2, Math.ceil(-Math.log10(step) - 1e-9));
  return '$' + v.toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

function timeTicks(x0, x1, max) {
  const step = TIME_STEPS.find(s => (x1 - x0) / s <= max) ?? 86400;
  const tz = new Date(x0 * 1000).getTimezoneOffset() * 60;  // align ticks to local clock time
  const ticks = [];
  for (let t = Math.ceil((x0 - tz) / step) * step + tz; t <= x1; t += step) ticks.push(t);
  return { ticks, step };
}

function nearest(pts, t) {
  let lo = 0, hi = pts.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (pts[mid][0] < t) lo = mid; else hi = mid;
  }
  return Math.abs(pts[lo][0] - t) <= Math.abs(pts[hi][0] - t) ? lo : hi;
}

function plotFrame(W, H, yt) {
  const labels = yt.ticks.map(v => fmtTick(v, yt.step));
  const m = { t: 10, r: 12, b: 28, l: Math.round(Math.max(...labels.map(l => l.length)) * 6.4 + 14) };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const Y = v => m.t + (1 - (v - yt.lo) / (yt.hi - yt.lo)) * ph;
  const svg = sv('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, tabindex: 0 });
  yt.ticks.forEach((v, i) => {
    const y = Math.round(Y(v)) + 0.5;
    svg.append(
      sv('line', { class: i ? 'grid-line' : 'axis-line', x1: m.l, x2: W - m.r, y1: y, y2: y }),
      sv('text', { class: 'tick', x: m.l - 8, y, dy: '0.32em', 'text-anchor': 'end' }, labels[i]));
  });
  return { svg, m, pw, ph, Y };
}

function xAxis(svg, m, H, x0, x1, X, pw) {
  const { ticks, step } = timeTicks(x0, x1, Math.max(2, Math.floor(pw / 92)));
  for (const t of ticks) svg.append(sv('text', { class: 'tick', x: X(t), y: H - 8, 'text-anchor': 'middle' }, fmtTime(t, step < 60)));
}

function placeTip(tip, x, W, top) {
  tip.hidden = false;
  const tw = tip.offsetWidth;
  let left = x + 14;
  if (left + tw > W) left = x - 14 - tw;
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${top}px`;
}

// Hover and keyboard layer shared by both charts. The hovered spot is remembered as a time (c.hoverT)
// so it survives the redraw every poll triggers; keyboard focus survives it too.
function interact(c, svg, n, { idxAt, tAt, paint, unpaint, keepT, hadFocus }) {
  let cur = -1;
  const show = i => { cur = i; c.hoverT = tAt(i); paint(i); };
  const hide = () => { cur = -1; c.hoverT = null; unpaint(); };
  svg.addEventListener('focus', () => show(cur >= 0 ? cur : n - 1));
  svg.addEventListener('blur', hide);
  svg.addEventListener('keydown', e => {
    const at = cur >= 0 ? cur : n - 1;
    const next = { ArrowLeft: at - 1, ArrowRight: at + 1, Home: 0, End: n - 1 }[e.key];
    if (next != null) { show(Math.min(Math.max(next, 0), n - 1)); e.preventDefault(); }
    if (e.key === 'Escape') hide();
  });
  const keep = keepT == null ? -1 : idxAt(keepT);
  if (hadFocus) {
    cur = keep >= 0 ? keep : n - 1;
    svg.focus({ preventScroll: true });
  } else if (keep >= 0) {
    show(keep);
  }
  return { show, hide };
}

// Cumulative spend: line with a crosshair tooltip that snaps to the nearest poll.
function drawLine(c, pts, opts) {
  const body = c.body, keepT = c.hoverT, hadFocus = body.contains(document.activeElement);
  body.replaceChildren();
  if (pts.length < 2 || pts[pts.length - 1][0] <= pts[0][0]) {
    body.append(h('div', { class: 'chart-empty', text: 'Waiting for a second poll…' }));
    return;
  }
  const W = Math.max(body.clientWidth, 260), H = 230;
  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { lo = Math.min(lo, p[1]); hi = Math.max(hi, p[1]); }
  const { svg, m, pw, ph, Y } = plotFrame(W, H, niceTicks(lo, hi, 4));
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0];
  const X = t => m.l + (t - x0) / (x1 - x0) * pw;
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${opts.label}: ${usd(pts[0][1])} to ${usd(pts[pts.length - 1][1])} over ${fmtDur(x1 - x0)}. Use arrow keys to step through polls.`);
  xAxis(svg, m, H, x0, x1, X, pw);
  svg.append(sv('path', { class: 'series-line', d: pts.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('') }));
  const end = pts[pts.length - 1];
  svg.append(sv('circle', { class: 'series-dot', cx: X(end[0]), cy: Y(end[1]), r: 4 }));
  const cross = sv('line', { class: 'cross', y1: m.t, y2: m.t + ph, visibility: 'hidden' });
  const hot = sv('circle', { class: 'series-dot', r: 5, visibility: 'hidden' });
  const hit = sv('rect', { class: 'hit', x: m.l - 10, y: 0, width: pw + 20, height: m.t + ph });
  svg.append(cross, hot, hit);
  const tip = h('div', { class: 'tip', hidden: true });
  body.append(svg, tip);

  const ui = interact(c, svg, pts.length, {
    idxAt: t => nearest(pts, t),
    tAt: i => pts[i][0],
    keepT, hadFocus,
    paint: i => {
      const p = pts[i], x = X(p[0]);
      for (const [k, v] of [['x1', x], ['x2', x], ['visibility', 'visible']]) cross.setAttribute(k, v);
      for (const [k, v] of [['cx', x], ['cy', Y(p[1])], ['visibility', 'visible']]) hot.setAttribute(k, v);
      const delta = i ? p[1] - pts[i - 1][1] : null;
      fill(tip,
        h('strong', { text: usd(p[1]) }),
        h('span', { class: 'tip-row' }, h('span', { class: 'tip-key' }), opts.series),
        h('span', { class: 'tip-row', text: fmtTime(p[0], true) }),
        delta != null && h('span', { class: 'tip-row', text: `${delta >= 0 ? '+' : ''}${usd(delta)} since the poll before` }));
      placeTip(tip, x, W, m.t);
    },
    unpaint: () => {
      cross.setAttribute('visibility', 'hidden');
      hot.setAttribute('visibility', 'hidden');
      tip.hidden = true;
    },
  });
  hit.addEventListener('pointermove', e => {
    const r = svg.getBoundingClientRect();
    ui.show(nearest(pts, x0 + ((e.clientX - r.left) * (W / r.width) - m.l) / pw * (x1 - x0)));
  });
  hit.addEventListener('pointerleave', ui.hide);
}

// Spend per time bucket (positive deltas between polls), one column per bucket.
function drawBars(c, b) {
  const body = c.body, keepT = c.hoverT, hadFocus = body.contains(document.activeElement);
  body.replaceChildren();
  const n = b.sums.length, W = Math.max(body.clientWidth, 260), H = 200;
  const max = Math.max(...b.sums);
  const { svg, m, pw, ph, Y } = plotFrame(W, H, niceTicks(0, max > 0 ? max : 0.001, 3));
  const span = n * b.width, band = pw / n, bw = Math.max(Math.min(24, band - 2), 1);
  const X = t => m.l + (t - b.start) / span * pw;
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `Key spend per ${fmtDur(b.width)}: ${usd(b.sums.reduce((a, v) => a + v, 0))} in total, at most ${usd(max)} in one interval. Use arrow keys to step through intervals.`);
  xAxis(svg, m, H, b.start, b.start + span, X, pw);
  const g = sv('g', { class: 'bars' }), bars = [];
  b.sums.forEach((v, i) => {
    const x = m.l + i * band + (band - bw) / 2, base = Y(0), top = Math.min(Y(v), base - 1), r = Math.min(4, bw / 2, base - top);
    bars.push(v > 0 ? g.appendChild(sv('path', {
      class: 'bar',
      d: `M${x},${base}V${top + r}A${r},${r} 0 0 1 ${x + r},${top}H${x + bw - r}A${r},${r} 0 0 1 ${x + bw},${top + r}V${base}Z`,
    })) : null);
  });
  svg.append(g);
  const tip = h('div', { class: 'tip', hidden: true });
  body.append(svg, tip);
  if (max <= 0) body.append(h('div', { class: 'chart-note', text: 'No key spend in this range' }));

  const ui = interact(c, svg, n, {
    idxAt: t => { const i = Math.floor((t - b.start) / b.width); return i >= 0 && i < n ? i : -1; },
    tAt: i => b.start + (i + 0.5) * b.width,
    keepT, hadFocus,
    paint: i => {
      g.classList.add('has-hot');
      bars.forEach((el, j) => el?.classList.toggle('hot', j === i));
      const t0 = b.start + i * b.width, secs = b.width < 60;
      fill(tip,
        h('strong', { text: usd(b.sums[i]) }),
        h('span', { class: 'tip-row' }, h('span', { class: 'tip-key' }), 'key spend'),
        h('span', { class: 'tip-row', text: `${fmtTime(t0, secs)} – ${fmtTime(t0 + b.width, secs)}` }));
      placeTip(tip, m.l + (i + 0.5) * band, W, m.t);
    },
    unpaint: () => {
      g.classList.remove('has-hot');
      tip.hidden = true;
    },
  });
  b.sums.forEach((_, i) => {
    const hit = sv('rect', { class: 'hit', x: m.l + i * band, y: 0, width: band, height: m.t + ph });
    hit.addEventListener('pointerenter', () => ui.show(i));
    hit.addEventListener('pointerleave', ui.hide);
    svg.append(hit);
  });
}

function bucketize(hist, x0, x1) {
  const width = TIME_STEPS.find(s => s >= Math.max((x1 - x0) / 48, S.data.config.interval)) ?? 86400;
  const tz = new Date(x0 * 1000).getTimezoneOffset() * 60;
  const start = Math.floor((x0 - tz) / width) * width + tz;
  const sums = new Array(Math.floor((x1 - start) / width) + 1).fill(0);
  for (let i = 1; i < hist.length; i++) {
    const [t, v] = hist[i];
    if (t < x0 || t > x1) continue;
    const dv = v - hist[i - 1][1];
    if (dv > 0) sums[Math.floor((t - start) / width)] += dv;
  }
  return { start, width, sums };
}

function chartCard(title) {
  const c = {
    title, table: false, draw: null, hoverT: null, lastW: 0,
    body: h('div', { class: 'chart' }),
    sub: h('p', { class: 'card-sub' }),
    stat: h('div', { class: 'head-stat' }),
    btn: h('button', { class: 'btn icon-btn sm', type: 'button', 'aria-pressed': 'false', 'aria-label': `Show ${title.toLowerCase()} as a table`, title: 'Table view' }, icon('table')),
  };
  c.card = h('section', { class: 'card' },
    h('div', { class: 'card-head' }, h('div', {}, h('h2', { class: 'card-title', text: title }), c.sub), h('div', { class: 'card-tools' }, c.stat, c.btn)),
    c.body);
  c.btn.addEventListener('click', () => {
    c.table = !c.table;
    c.btn.setAttribute('aria-pressed', String(c.table));
    c.btn.setAttribute('aria-label', c.table ? `Show ${title.toLowerCase()} as a chart` : `Show ${title.toLowerCase()} as a table`);
    c.btn.replaceChildren(icon(c.table ? 'chart-line' : 'table'));
    c.draw?.();
    fadeIn(c.body);
  });
  new ResizeObserver(() => {
    if (c.body.clientWidth !== c.lastW) { c.lastW = c.body.clientWidth; if (!c.table) c.draw?.(); }
  }).observe(c.body);
  return c;
}

function showTable(c, head, rows, note) {
  fill(c.body, h('div', { class: 'table-wrap' }, table(head, rows)), note && h('p', { class: 'card-sub', text: note }));
}

function lineTable(c, pts) {
  const rows = [];
  for (let i = pts.length - 1; i >= Math.max(pts.length - 200, 0); i--) {
    const d = i ? pts[i][1] - pts[i - 1][1] : null;
    rows.push([fmtTime(pts[i][0], true), usd(pts[i][1]), d == null ? dim('—') : `${d >= 0 ? '+' : ''}${usd(d)}`]);
  }
  showTable(c, [['Time', ''], ['Spend', 'num'], ['Change', 'num']], rows, pts.length > 200 ? `Latest 200 of ${pts.length} polls` : null);
}

function renderSpendChart(c, pts, label) {
  const first = pts[0], last = pts[pts.length - 1];
  c.sub.textContent = 'Cumulative spend, one point per poll';
  c.stat.replaceChildren(...(last ? [h('strong', { text: usd(last[1]) }),
    pts.length > 1 ? `+${usd(Math.max(last[1] - first[1], 0))} in range` : 'latest'] : []));
  c.draw = () => (c.table ? lineTable(c, pts) : drawLine(c, pts, { label, series: label.toLowerCase() }));
  c.draw();
}

function renderCharts() {
  const hist = S.history, span = RANGES.find(([k]) => k === S.range)[1];
  const last = hist[hist.length - 1];
  const x1 = last ? last[0] : nowS();
  const inRange = hist.filter(p => p[0] >= x1 - span);
  $('range-note').textContent = `${inRange.length} of ${hist.length} polls in range · history lives in the server's memory`;

  renderSpendChart(charts.key, inRange, 'Key spend');

  const bars = charts.bars;
  if (inRange.length < 2) {
    bars.sub.textContent = 'Spend added between polls';
    bars.stat.replaceChildren();
    bars.draw = () => bars.body.replaceChildren(h('div', { class: 'chart-empty', text: 'Waiting for a second poll…' }));
  } else {
    const b = bucketize(hist, inRange[0][0], x1);
    const total = b.sums.reduce((a, v) => a + v, 0);
    const active = b.sums.filter(v => v > 0).length;
    bars.sub.textContent = `Spend added per ${fmtDur(b.width)}`;
    bars.stat.replaceChildren(h('strong', { text: usd(total) }), `${active} of ${b.sums.length} intervals active`);
    bars.draw = () => (bars.table
      ? showTable(bars, [['Interval start', ''], ['Spend', 'num']],
        b.sums.map((v, i) => [fmtTime(b.start + i * b.width, b.width < 60), v ? usd(v) : dim(usd(0))]).reverse())
      : drawBars(bars, b));
  }
  bars.draw();
}

function renderRange() {
  $('range').replaceChildren(...RANGES.map(([k]) => {
    const btn = h('button', { type: 'button', 'aria-pressed': String(k === S.range), text: k });
    btn.addEventListener('click', () => {
      S.range = k;
      save('keymeter-range', k);
      renderRange();
      if (S.data?.summary.key_info) {
        renderCharts();
        for (const c of Object.values(charts)) fadeIn(c.body);
      }
    });
    return btn;
  }));
}

// ---------------------------------------------------------------- init

function init() {
  if (!RANGES.some(([k]) => k === S.range)) S.range = '1h';
  if (!THEMES.includes(S.theme)) S.theme = 'auto';
  applyTheme();
  renderAlertsBtn();
  renderRange();
  charts = { key: chartCard('Key spend'), bars: chartCard('Key spend per interval') };
  $('charts').append(charts.key.card, charts.bars.card);

  $('theme-btn').addEventListener('click', () => {
    S.theme = THEMES[(THEMES.indexOf(S.theme) + 1) % THEMES.length];
    save('keymeter-theme', S.theme);
    applyTheme(true);
  });
  $('alerts-btn').addEventListener('click', toggleAlerts);
  $('poll-now').addEventListener('click', pollNow);
  // browsers start audio suspended until the page gets a gesture
  document.addEventListener('pointerdown', () => audio?.resume(), { once: true });

  setInterval(() => {
    if (!S.data) return;
    for (const el of document.querySelectorAll('.tick-val')) el.textContent = fmtDur(tickValue(el));
    renderLive();
  }, 1000);
  loop();
}

init();
