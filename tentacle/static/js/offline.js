// Tentacle — Offline: movies and episodes saved on this device to watch with
// no internet (Nov 2026).
//
// Saved from Library (a title's Save offline, a show's or a season's: every
// episode) or from Jellyfin in a browser (More → Save offline opens
// #offline/save-<item>). The video is the person's own link
// (/api/offline/items/<id>/video: Jellyfin's Download, a provider's title
// through Tentacle's relay; anything this browser can't play as it is, an
// MKV say, made into an MP4 by the server as it sends it), written into the
// browser's storage for this address by offline-worker.js, resumed from the
// byte it has after any interruption. What is known about each title (name, picture,
// where they stopped) is in IndexedDB; the files in OPFS ('offline/<id>.<ext>').
//
// With no internet the app opens straight to this page (app.js), and a title
// plays from the file. Where they stopped goes back to Jellyfin when the
// device is online again (/api/offline/progress).
//
// iPhone: a browser tab (Safari, Chrome) saves as well as the home-screen app
// (each keeps its own), only while it is open with the screen on: the screen
// is kept awake while one runs, and it carries on when it is opened again.
// iOS may clear what a site saved if it isn't opened for about a week (the
// page says so).
'use strict';

const OFF_DB = 'tentacle-offline';
const OFF_STORE = 'titles';
const OFF_USER_KEY = 'tentacle-offline-user';
const OFF_RETRY_MS = [5000, 15000, 30000, 60000, 120000];

const off = {
  db: null,
  titles: [],          // this person's, newest first
  worker: null,
  current: null,       // id being saved now
  live: {},            // id -> {received, total, at, rate}
  retries: {},         // id -> attempts since the last progress
  jobs: {},            // id -> which making of the server's MP4 its file holds
  retryAt: {},         // id -> when it may try again (ms): a broken one waits
  retryTimer: null,
  wakeLock: null,
  player: null,        // {id, video, url, saveTimer}
  syncing: false,
  offlineMode: false,  // opened with no internet (app.js)
};

// ── Storage ────────────────────────────────────────────────────────────────

function _offOpen() {
  if (off.db) return Promise.resolve(off.db);
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(OFF_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(OFF_STORE)) db.createObjectStore(OFF_STORE, { keyPath: 'id' });
    };
    req.onsuccess = () => { off.db = req.result; resolve(off.db); };
    req.onerror = () => reject(req.error);
  });
}

async function _offTx(mode, fn) {
  const db = await _offOpen();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(OFF_STORE, mode);
    const store = tx.objectStore(OFF_STORE);
    let out;
    Promise.resolve(fn(store)).then(v => { out = v; });
    tx.oncomplete = () => resolve(out);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

function _offReq(req) {
  return new Promise((resolve, reject) => { req.onsuccess = () => resolve(req.result); req.onerror = () => reject(req.error); });
}

async function _offAll() {
  return _offTx('readonly', store => _offReq(store.getAll()));
}

async function _offGet(id) {
  return _offTx('readonly', store => _offReq(store.get(id)));
}

async function _offPut(rec) {
  await _offTx('readwrite', store => { store.put(rec); });
  return rec;
}

async function _offUpdate(id, change) {
  const rec = await _offGet(id);
  if (!rec) return null;
  Object.assign(rec, change);
  return _offPut(rec);
}

async function _offDelete(id) {
  await _offTx('readwrite', store => { store.delete(id); });
}

function _offFile(rec) {
  return `${rec.id}.${(rec.container || 'mp4').replace(/[^a-z0-9]/gi, '') || 'mp4'}`;
}

async function _offFileHandle(rec) {
  const root = await navigator.storage.getDirectory();
  const dir = await root.getDirectoryHandle('offline', { create: true });
  return dir.getFileHandle(_offFile(rec));
}

// ── Who and where ──────────────────────────────────────────────────────────

function _offUser() {
  const u = (typeof state !== 'undefined' && state.currentUser) || null;
  if (u && u.jellyfin_user_id) {
    try { localStorage.setItem(OFF_USER_KEY, JSON.stringify({ id: u.jellyfin_user_id, name: u.display_name || '' })); } catch (e) { /* private mode */ }
    return u.jellyfin_user_id;
  }
  try { return (JSON.parse(localStorage.getItem(OFF_USER_KEY) || 'null') || {}).id || ''; } catch (e) { return ''; }
}

function offSupported() {
  return !!(window.indexedDB && navigator.storage && navigator.storage.getDirectory && window.Worker);
}

function _offIsIos() {
  return /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function _offStandalone() {
  return !!(navigator.standalone || (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches));
}

// An iPhone browser tab (not the home-screen app): iOS may clear its storage.
function _offIosBrowser() {
  return _offIsIos() && !_offStandalone();
}

// What this browser plays besides H.264 and AAC (HEVC, an iPhone does): the
// server makes the rest into what it plays (services/offline_convert.py).
function _offCaps() {
  const v = document.createElement('video');
  return v.canPlayType('video/mp4; codecs="hvc1.1.6.L93.B0"') ? 'hevc' : '';
}

// ── Words ──────────────────────────────────────────────────────────────────

function _offEsc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function _offSize(bytes) {
  if (!bytes) return '';
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

function _offTitle(rec) {
  if (rec.kind === 'episode') {
    const se = rec.season != null && rec.episode != null ? `S${rec.season} E${rec.episode}` : '';
    return { main: rec.series || rec.name, sub: [se, rec.name].filter(Boolean).join(' · ') };
  }
  return { main: rec.name, sub: rec.year ? String(rec.year) : '' };
}

function _offLeft(rec) {
  if (!rec.runtime_ticks || !rec.position_ticks || rec.played) return '';
  const left = Math.round((rec.runtime_ticks - rec.position_ticks) / 6e8);
  return left > 0 ? `${left} min left` : '';
}

// ── The page ───────────────────────────────────────────────────────────────

async function loadOffline(tab) {
  const el = document.getElementById('offline-list');
  if (!el) return;
  if (!offSupported()) {
    el.innerHTML = window.isSecureContext === false
      ? '<p class="off-status">To save titles here, open Tentacle at its https:// address.</p>'
      : '<p class="off-status">This browser can’t keep videos to watch offline.</p>';
    return;
  }
  if (tab && /^save-[0-9a-f-]{32,36}$/i.test(tab)) {
    // From Jellyfin's "Save offline": save it, then show the list.
    try { history.replaceState(null, '', '#offline'); } catch (e) { /* sandboxed */ }
    offlineSave(tab.slice(5));
  }
  await offRefresh();
  offResume();
  offSyncProgress();
  // Saved before their subtitles were ready (or saved before subtitles came
  // with titles): get them while there is internet.
  if (navigator.onLine) {
    off.titles.filter(r => r.state === 'done' && !r.subtitles_ready).forEach(r => _offSubtitles(r.id));
  }
}

async function offRefresh() {
  let all = [];
  try { all = await _offAll(); } catch (e) { all = []; }
  const me = _offUser();
  off.titles = all.filter(r => !me || !r.user || r.user === me).sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
  offRender();
  _offNavCount();
}

function _offNavCount() {
  const badge = document.getElementById('offline-badge');
  if (!badge) return;
  const busy = off.titles.filter(r => r.state === 'saving' || r.state === 'downloading' || r.state === 'waiting').length;
  badge.textContent = busy;
  badge.style.display = busy ? '' : 'none';
}

function offRender() {
  const el = document.getElementById('offline-list');
  const head = document.getElementById('offline-status');
  if (!el) return;
  const titles = off.titles;
  const busy = titles.filter(r => r.state === 'saving' || r.state === 'waiting' || r.state === 'downloading');
  if (head) {
    let line = '';
    if (off.offlineMode || !navigator.onLine) line = 'No internet. What you saved here plays.';
    else if (busy.length) line = busy.length === 1 ? 'Saving 1 title. Keep this page open.' : `Saving ${busy.length} titles. Keep this page open.`;
    head.textContent = line;
    head.style.display = line ? '' : 'none';
  }
  const note = document.getElementById('offline-note');
  if (note) {
    // iPhone browser tab: iOS clears a site's storage it hasn't seen for a while.
    note.textContent = _offIosBrowser() && titles.length ? OFF_IOS_WORDS : '';
    note.style.display = note.textContent ? '' : 'none';
  }
  if (!titles.length) {
    el.innerHTML = `<p class="off-status">Nothing saved on this ${_offIsIos() || window.innerWidth <= 768 ? 'phone' : 'device'} yet. In Library, open a movie or show and tap <strong>Save offline</strong>.</p>`;
    return;
  }
  el.innerHTML = titles.map(rec => {
    const t = _offTitle(rec);
    const pic = rec.poster_url ? `<img class="off-pic" src="${_offEsc(rec.poster_url)}" alt="">` : '<div class="off-pic off-pic-none"></div>';
    let meta = '';
    let bar = '';
    if (rec.state === 'done') {
      meta = [_offLeft(rec) || (rec.played ? 'Watched' : ''), _offSize(rec.size)].filter(Boolean).join(' · ');
    } else {
      const live = off.live[rec.id] || {};
      const got = live.received ?? rec.received ?? 0;
      const pct = _offPct(rec, live, got);
      const words = rec.state === 'failed' ? (rec.error || 'Stopped')
        : rec.state === 'waiting' ? (rec.error || 'Waiting to carry on')
        : off.current === rec.id ? _offProgressWords(rec, live, got)
        : 'Waiting its turn';
      meta = words;
      bar = `<div class="off-bar"><div class="off-bar-fill" style="width:${pct}%"></div></div>`;
    }
    const play = rec.state === 'done'
      ? `onclick="offPlay('${rec.id}')" role="button" tabindex="0" onkeydown="if(event.key==='Enter')offPlay('${rec.id}')"` : '';
    const action = rec.state === 'failed' && !rec.fatal
      ? `<button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();offRetry('${rec.id}')">Try again</button>` : '';
    return `<div class="off-item${rec.state === 'done' ? ' off-ready' : ''}" data-id="${rec.id}" ${play}>
      ${pic}
      <div class="off-text">
        <div class="off-name">${_offEsc(t.main)}</div>
        ${t.sub ? `<div class="off-sub">${_offEsc(t.sub)}</div>` : ''}
        <div class="off-meta" id="off-meta-${rec.id}">${_offEsc(meta)}</div>
        ${bar}
      </div>
      <div class="off-actions">${action}
        <button class="off-remove" title="Remove from this device" aria-label="Remove ${_offEsc(t.main)} from this device"
          onclick="event.stopPropagation();offRemove('${rec.id}', this)">✕</button></div>
    </div>`;
  }).join('');
}

// How far it got: of its size, or (an MP4 the server is still making) of
// about how big it will be, never 100% before it has finished.
function _offPct(rec, live, got) {
  const total = live.total || 0;
  if (total) return Math.min(100, Math.floor(got * 100 / total));
  const about = live.estimate || rec.size || 0;
  return about ? Math.min(99, Math.floor(got * 100 / about)) : 0;
}

function _offProgressWords(rec, live, got) {
  const total = live.total || 0;
  const about = live.estimate || rec.size || 0;
  if (!got && !total) return 'Starting…';
  if (total) return `${_offPct(rec, live, got)}% · ${_offSize(got)} of ${_offSize(total)}`;
  if (about) return `${_offPct(rec, live, got)}% · ${_offSize(got)} of about ${_offSize(Math.max(about, got))}`;
  return `${_offSize(got)} so far`;
}

function _offShowProgress(id) {
  const rec = off.titles.find(r => r.id === id);
  const live = off.live[id];
  const row = document.querySelector(`.off-item[data-id="${id}"]`);
  if (!rec || !live || !row) return;
  const meta = row.querySelector('.off-meta');
  const fill = row.querySelector('.off-bar-fill');
  if (meta) meta.textContent = _offProgressWords(rec, live, live.received || 0);
  if (fill) fill.style.width = `${_offPct(rec, live, live.received || 0)}%`;
}

// ── Saving ─────────────────────────────────────────────────────────────────

// Whether this person may save offline (asked once a page load).
let _offWhere = null;
function offCanDownload() {
  if (!_offWhere) {
    _offWhere = fetch('/api/offline/where', { credentials: 'same-origin' })
      .then(r => (r.ok ? r.json() : {})).catch(() => ({}));
  }
  return _offWhere;
}

// The Save offline buttons under `root` show for people who may save offline.
async function offShowDownloadButtons(root) {
  if (!root || !offSupported()) return;
  const where = await offCanDownload();
  if (!where.can_download) return;
  root.querySelectorAll('.off-dl').forEach(b => { b.style.display = ''; });
}

// Save a Jellyfin movie, episode, show or season on this device (Library's
// Save offline, Jellyfin's Save offline). A show or a season: every episode
// of it not saved here yet (`season`: that season of the show).
async function offlineSave(itemId, btn, season) {
  const id = String(itemId || '').replace(/-/g, '').toLowerCase();
  if (!/^[0-9a-f]{32}$/.test(id)) return;
  if (!offSupported()) { toast('This browser can’t keep videos to watch offline.', 'error', 6000); return; }
  const have = await _offGet(id).catch(() => null);
  if (have) {
    toast(have.state === 'done' ? 'It’s already saved on this device (Offline).' : 'It’s already saving (Offline).');
    if (state.currentPage !== 'offline') showPage('offline');
    return;
  }
  if (btn) btn.disabled = true;
  let info;
  try {
    info = await api(`/api/offline/items/${id}${season != null ? `?season=${encodeURIComponent(season)}` : ''}`);
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message || 'It can’t be saved right now.', 'error', 7000);
    return;
  }
  try { if (navigator.storage.persist) await navigator.storage.persist(); } catch (e) { /* asked once */ }
  const can = _offCaps();
  const user = _offUser();
  let added = 0;
  let name;
  if (info.episodes) {
    // A show or a season: each episode not on this device yet, in order.
    const all = await _offAll().catch(() => []);
    const here = new Set(all.map(r => r.id));
    const todo = info.episodes.filter(ep => !here.has(ep.id));
    if (!todo.length) {
      if (btn) btn.disabled = false;
      toast(`${info.name || 'They'}: every episode is already on this device (Offline).`);
      return;
    }
    if (!await _offRoomFor(todo)) { if (btn) btn.disabled = false; return; }
    const now = Date.now();
    for (let i = 0; i < todo.length; i++) {
      await _offPut({ ..._offNewRecord(todo[i], user, can, '', now - i), batch: now, order: i });
    }
    added = todo.length;
    name = info.season != null ? `${info.name} season ${info.season}` : info.name;
  } else {
    let poster = '';
    try {
      const r = await fetch(`/api/offline/items/${id}/poster`, { credentials: 'same-origin' });
      if (r.ok) poster = await _offDataUrl(await r.blob());
    } catch (e) { /* no picture */ }
    await _offPut(_offNewRecord({ ...info, id }, user, can, poster, Date.now()));
    added = 1;
    name = info.kind === 'episode' ? (info.series || info.name) : info.name;
  }
  if (btn) {
    btn.disabled = false;
    const label = btn.querySelector('span');
    if (label) label.textContent = 'Saving';
  }
  toast(added === 1 ? `Saving ${name}. It’s on the Offline page.`
    : `Saving ${added} episodes of ${name}. They’re on the Offline page.`);
  await offRefresh();
  offResume();
}

function _offNewRecord(info, user, can, poster, added) {
  return {
    ...info, user, can, state: 'waiting', error: '', received: 0, fatal: false, job: '',
    poster_url: poster, added_at: added, watched_at: 0, synced_at: 0,
  };
}

// Room on this device for a show's episodes? Said plainly before starting.
async function _offRoomFor(eps) {
  const need = eps.reduce((n, ep) => n + (ep.size || (ep.runtime_ticks || 0) / 1e7 * (1 << 20)), 0);
  try {
    const est = await navigator.storage.estimate();
    const free = (est.quota || 0) - (est.usage || 0);
    if (est.quota && need > free) {
      toast(`Not enough space on this device: ${eps.length} episodes need about ${_offSize(need)}, ${_offSize(Math.max(0, free))} is free.`, 'error', 9000);
      return false;
    }
  } catch (e) { /* no estimate: go ahead */ }
  return true;
}

function _offDataUrl(blob) {
  // Kept as text in IndexedDB: Safari's IndexedDB has refused Blobs before.
  return new Promise(resolve => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || ''));
    fr.onerror = () => resolve('');
    fr.readAsDataURL(blob);
  });
}

const OFF_IOS_WORDS = 'On iPhone, what you save in a browser can be cleared by iOS if you don’t open Tentacle for about a week.';

// The next title waiting, one at a time.
async function offResume() {
  if (!offSupported() || off.current || !navigator.onLine) return;
  const now = Date.now();
  // Oldest first; a show's episodes in their order
  const due = off.titles.filter(r => r.state === 'waiting' || r.state === 'saving' || r.state === 'downloading')
    .sort((a, b) => ((a.batch ?? a.added_at) - (b.batch ?? b.added_at)) || ((a.order || 0) - (b.order || 0)));
  const next = due.find(r => !(off.retryAt[r.id] > now));
  if (!next) {
    if (!due.length) _offWake(false);
    return;
  }
  _offStart(next);
}

function _offWorker() {
  if (!off.worker) {
    off.worker = new Worker('/static/js/offline-worker.js?v=3');
    off.worker.onmessage = ev => _offMessage(ev.data || {});
  }
  return off.worker;
}

async function _offStart(rec) {
  off.current = rec.id;
  await _offUpdate(rec.id, { state: 'saving', error: '' });
  rec.state = 'saving';
  _offWake(true);
  off.live[rec.id] = { received: rec.received || 0, total: 0, estimate: rec.size || 0 };
  offRender();
  if (!rec.poster_url) _offPoster(rec);
  const can = rec.can != null ? rec.can : _offCaps();
  _offWorker().postMessage({ cmd: 'start', id: rec.id, job: rec.job || '', file: _offFile(rec),
    url: `/api/offline/items/${rec.id}/video${can ? `?can=${encodeURIComponent(can)}` : ''}` });
}

// An episode saved with its show gets its picture when its turn comes.
async function _offPoster(rec) {
  try {
    const r = await fetch(`/api/offline/items/${rec.id}/poster`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const poster = await _offDataUrl(await r.blob());
    await _offUpdate(rec.id, { poster_url: poster });
    const t = off.titles.find(x => x.id === rec.id);
    if (t) t.poster_url = poster;
    const img = document.querySelector(`.off-item[data-id="${rec.id}"] .off-pic`);
    if (img && img.tagName !== 'IMG') offRender();
  } catch (e) { /* no picture */ }
}

async function _offMessage(msg) {
  const id = msg.id;
  if (msg.type === 'job') {
    // Which making of the server's MP4 this file holds (offline-worker.js);
    // written with every update below too (they read and write the whole record).
    off.jobs[id] = msg.job;
    _offUpdate(id, { job: msg.job }).catch(() => {});
    return;
  }
  if (msg.type === 'progress') {
    const before = off.live[id] || {};
    off.live[id] = { received: msg.received, total: msg.total || 0, estimate: msg.estimate || before.estimate || 0 };
    if (msg.received > (before.received || 0)) off.retries[id] = 0;
    _offShowProgress(id);
    // Kept now and then, for the list after the app was closed.
    if (!before.saved || Date.now() - before.saved > 5000) {
      off.live[id].saved = Date.now();
      _offUpdate(id, _offWithJob(id, msg.total || msg.estimate ? { received: msg.received, size: msg.total || msg.estimate } : { received: msg.received })).catch(() => {});
    } else {
      off.live[id].saved = before.saved;
    }
    return;
  }
  if (msg.type === 'removed') return;
  if (off.current === id) off.current = null;
  if (msg.type === 'done') {
    await _offUpdate(id, _offWithJob(id, { state: 'done', received: msg.received, size: msg.total || msg.received, error: '' }));
    delete off.live[id];
    const rec = off.titles.find(r => r.id === id);
    if (rec) toast(`${_offTitle(rec).main} is ready to watch offline.`);
    _offSubtitles(id);
  } else if (msg.type === 'failed') {
    await _offFailed(id, msg);
  }
  await offRefresh();
  offResume();
}

async function _offFailed(id, msg) {
  const change = msg.received != null ? { received: msg.received } : {};
  if (msg.reason === 'stopped') {
    // Stopped on purpose (removed, or the page is going away): nothing to say.
    return;
  }
  if (msg.reason === 'unsupported') {
    Object.assign(change, { state: 'failed', fatal: true, error: 'This browser can’t save videos. Update it, or use another browser.' });
  } else if (msg.reason === 'space') {
    Object.assign(change, { state: 'failed', fatal: false,
      error: msg.need ? `Not enough space on this device: it needs ${_offSize(msg.need)}, ${_offSize(Math.max(0, msg.free))} is free.`
        : 'This device is full. Make some room, then tap Try again.' });
  } else if (msg.status === 403 || msg.status === 404 || msg.status === 400 || msg.status === 409) {
    Object.assign(change, { state: 'failed', fatal: msg.status !== 409, error: msg.detail || 'It can’t be saved.' });
  } else if (msg.status === 401) {
    Object.assign(change, { state: 'failed', fatal: false, error: 'Sign in again, then tap Try again.' });
  } else {
    // A lost connection, a busy provider, a break: carry on by itself.
    const n = (off.retries[id] || 0) + 1;
    off.retries[id] = n;
    const wait = OFF_RETRY_MS[Math.min(n - 1, OFF_RETRY_MS.length - 1)];
    const busy = msg.status === 503;
    const words = busy ? 'The server is busy (someone is watching). It carries on by itself.'
      : !navigator.onLine ? 'No internet. It carries on when you’re back online.'
      : msg.reason === 'short' ? ''
      : (msg.status >= 500 && msg.detail) ? `${msg.detail} It tries again by itself.`
      : 'The connection broke. It carries on by itself.';
    Object.assign(change, { state: 'waiting', error: words });
    off.retryAt[id] = Date.now() + wait;
    clearTimeout(off.retryTimer);
    off.retryTimer = setTimeout(() => offResume(), wait + 50);
  }
  await _offUpdate(id, _offWithJob(id, change));
}

function _offWithJob(id, change) {
  return off.jobs[id] != null ? { ...change, job: off.jobs[id] } : change;
}

async function offRetry(id) {
  off.retries[id] = 0;
  delete off.retryAt[id];
  await _offUpdate(id, { state: 'waiting', error: '', fatal: false });
  await offRefresh();
  offResume();
}

async function offRemove(id, btn) {
  if (btn && btn.dataset.armed !== '1') {
    btn.dataset.armed = '1';
    btn.textContent = 'Remove?';
    btn.classList.add('armed');
    setTimeout(() => { if (btn.dataset.armed === '1') { btn.dataset.armed = ''; btn.textContent = '✕'; btn.classList.remove('armed'); } }, 4000);
    return;
  }
  const rec = await _offGet(id).catch(() => null);
  if (!rec) return;
  if (off.player && off.player.id === id) offClosePlayer();
  _offWorker().postMessage({ cmd: 'remove', id, file: _offFile(rec) });
  if (off.current === id) off.current = null;
  delete off.live[id];
  await _offDelete(id);
  await offRefresh();
  offResume();
}

// Keep the screen on while a title saves (an iPhone saves only while the
// page is open with the screen on).
async function _offWake(on) {
  try {
    if (on && !off.wakeLock && navigator.wakeLock && document.visibilityState === 'visible') {
      off.wakeLock = await navigator.wakeLock.request('screen');
      off.wakeLock.addEventListener('release', () => { off.wakeLock = null; });
    } else if (!on && off.wakeLock) {
      await off.wakeLock.release();
      off.wakeLock = null;
    }
  } catch (e) { off.wakeLock = null; }
}

// ── Watching ───────────────────────────────────────────────────────────────

async function offPlay(id) {
  const rec = await _offGet(id).catch(() => null);
  if (!rec || rec.state !== 'done') return;
  let file;
  try { file = await (await _offFileHandle(rec)).getFile(); } catch (e) {
    toast('That title is missing from this device. Remove it and save it again.', 'error', 8000);
    return;
  }
  offClosePlayer();
  const url = URL.createObjectURL(file);
  const wrap = document.createElement('div');
  wrap.className = 'off-player';
  wrap.id = 'off-player';
  const t = _offTitle(rec);
  wrap.innerHTML = `<div class="off-player-top"><div class="off-player-name">${_offEsc(t.main)}${t.sub ? ` <span>${_offEsc(t.sub)}</span>` : ''}</div>
    <button class="off-player-close" onclick="offClosePlayer()" aria-label="Close">✕</button></div>
    <video controls playsinline autoplay preload="auto"></video>
    <div class="off-player-note" id="off-player-note" style="display:none"></div>`;
  document.body.appendChild(wrap);
  const video = wrap.querySelector('video');
  if (!rec.subtitles_ready && navigator.onLine) {
    const got = await _offSubtitles(id);
    if (got) Object.assign(rec, { subtitles: got.tracks, subtitle_mode: got.mode, subtitles_ready: got.ready });
  }
  _offAddTracks(video, rec);
  off.player = { id, video, url, saveTimer: null };
  video.addEventListener('loadedmetadata', () => {
    const at = rec.played ? 0 : (rec.position_ticks || 0) / 1e7;
    if (at > 5 && at < video.duration - 30) video.currentTime = at;
  }, { once: true });
  video.addEventListener('error', () => {
    const note = document.getElementById('off-player-note');
    if (note) {
      note.textContent = 'This device can’t play this file. Remove it and save it again.';
      note.style.display = '';
    }
  });
  video.addEventListener('pause', () => _offSavePosition(false));
  video.addEventListener('ended', () => _offSavePosition(true));
  off.player.saveTimer = setInterval(() => _offSavePosition(false), 10000);
  video.src = url;
  video.play().catch(() => { /* the person taps play */ });
}

// The subtitles a saved title takes with it (the person's languages, as
// WebVTT: GET /api/offline/items/{id}/subtitles). Kept in its record; asked
// again while online until the server says they are all ready.
async function _offSubtitles(id) {
  try {
    const r = await fetch(`/api/offline/items/${id}/subtitles`, { credentials: 'same-origin' });
    if (!r.ok) return null;
    const got = await r.json();
    await _offUpdate(id, { subtitles: got.tracks, subtitle_mode: got.mode, subtitles_ready: !!got.ready });
    // Still being read on the server: ask again while this page is open.
    if (!got.ready) setTimeout(() => _offSubtitles(id), 30000);
    return got;
  } catch (e) {
    return null;     // offline: what was kept stays
  }
}

function _offAddTracks(video, rec) {
  (rec.subtitles || []).forEach((t, i) => {
    const track = document.createElement('track');
    track.kind = 'subtitles';
    track.label = t.label;
    track.srclang = t.language;
    track.src = URL.createObjectURL(new Blob([t.vtt], { type: 'text/vtt' }));
    if (i === 0 && rec.subtitle_mode === 'always') track.default = true;
    video.appendChild(track);
  });
}

async function _offSavePosition(ended) {
  const p = off.player;
  if (!p || !p.video) return;
  const v = p.video;
  if (!v.duration || isNaN(v.duration) || (!ended && v.currentTime < 1)) return;
  const played = ended || v.currentTime >= v.duration * 0.92;
  await _offUpdate(p.id, {
    position_ticks: played ? 0 : Math.floor(v.currentTime * 1e7),
    played, watched_at: Date.now(),
  }).catch(() => {});
}

function offClosePlayer() {
  const p = off.player;
  if (!p) return;
  _offSavePosition(false).finally(() => { offRefresh(); offSyncProgress(); });
  clearInterval(p.saveTimer);
  try { p.video.pause(); p.video.removeAttribute('src'); p.video.load(); } catch (e) { /* gone */ }
  URL.revokeObjectURL(p.url);
  document.getElementById('off-player')?.remove();
  off.player = null;
}

// ── Back to Jellyfin ───────────────────────────────────────────────────────

// Where they stopped, for each title watched here since the last time it
// was sent, into Jellyfin (it keeps its own answer if that is newer).
async function offSyncProgress() {
  if (off.syncing || !navigator.onLine || !offSupported()) return;
  off.syncing = true;
  try {
    const all = await _offAll();
    const me = _offUser();
    const due = all.filter(r => r.watched_at && r.watched_at > (r.synced_at || 0) && (!me || !r.user || r.user === me));
    if (!due.length) return;
    const res = await fetch('/api/offline/progress', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ items: due.map(r => ({ id: r.id, position_ticks: r.position_ticks || 0, played: !!r.played, at: r.watched_at })) }),
    });
    if (!res.ok) return;
    const now = Date.now();
    for (const r of due) await _offUpdate(r.id, { synced_at: now });
  } catch (e) {
    /* still offline: next time */
  } finally {
    off.syncing = false;
  }
}

// ── With no internet ───────────────────────────────────────────────────────

// Opened with no internet (app.js): only this page, from this device.
async function offlineOnly() {
  off.offlineMode = true;
  document.body.classList.add('offline-mode');
  const overlay = document.getElementById('login-overlay');
  if (overlay) overlay.style.display = 'none';
  showPage('offline');
}

window.addEventListener('online', () => {
  if (off.offlineMode) {
    // Back online: the whole app again (signed in as before).
    location.reload();
    return;
  }
  offSyncProgress();
  off.retryAt = {};
  offResume();
  if (typeof state !== 'undefined' && state.currentPage === 'offline') offRender();
});
window.addEventListener('offline', () => {
  if (typeof state !== 'undefined' && state.currentPage === 'offline') offRender();
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && off.current) _offWake(true);
});

// Saving carries on whenever Tentacle is open, whatever page is showing.
document.addEventListener('DOMContentLoaded', () => {
  if (!offSupported()) return;
  setTimeout(async () => {
    try {
      if (!off.titles.length) {
        const all = await _offAll();
        const me = _offUser();
        off.titles = all.filter(r => !me || !r.user || r.user === me).sort((a, b) => (b.added_at || 0) - (a.added_at || 0));
        _offNavCount();
      }
      offResume();
      offSyncProgress();
      _offNavShown();
    } catch (e) { /* no storage */ }
  }, 3000);
});

// Offline in the sidebar: for people who may save offline, or who have
// something saved here (it plays whatever "Can save offline" says now).
async function _offNavShown() {
  const nav = document.querySelector('.nav-item[data-page="offline"]');
  if (!nav || off.offlineMode || !navigator.onLine) return;
  const where = await offCanDownload();
  if (where && where.can_download === false && !off.titles.length) nav.style.display = 'none';
}
