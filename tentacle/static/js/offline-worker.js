// Tentacle Offline: saves one title's video into this device's own storage
// (OPFS), a piece at a time, never holding the film in memory.
//
// Started by offline.js with {cmd: 'start', id, url, file, job}. A title the
// browser can't play as it is comes as an MP4 the server makes while it
// sends it (services/offline_convert.py): no size until it is finished
// (X-Offline-Estimate: about how big), its first bytes taken again at the
// end, and X-Offline-Job says which making it is (another one: start over). It writes what
// arrives at the byte it belongs to (a sync access handle, only possible in a
// worker) and, after an interruption, asks for the rest from the byte the
// file already has (Range), so nothing is fetched twice. It reports
// {type: 'progress' | 'done' | 'failed', ...}; offline.js decides whether to
// try again (a lost connection, a busy server) or give up (no permission).
'use strict';

let running = null;   // {id, controller, handle}
const HEAD = 65536;   // the first bytes of an MP4 the server made, taken again at the end

async function fileHandle(name) {
  const root = await navigator.storage.getDirectory();
  const dir = await root.getDirectoryHandle('offline', { create: true });
  return dir.getFileHandle(name, { create: true });
}

function totalOf(res, from) {
  const cr = res.headers.get('content-range') || '';
  const m = /\/(\d+)\s*$/.exec(cr);
  if (m) return Number(m[1]);
  const len = Number(res.headers.get('content-length') || 0);
  return len ? (res.status === 206 ? from + len : len) : 0;
}

async function start(msg) {
  const { id, url, file } = msg;
  let job = msg.job || '';
  const controller = new AbortController();
  let handle = null;
  running = { id, controller, handle: null };
  try {
    const fh = await fileHandle(file);
    if (typeof fh.createSyncAccessHandle !== 'function') {
      postMessage({ type: 'failed', id, fatal: true, reason: 'unsupported' });
      return;
    }
    handle = await fh.createSyncAccessHandle();
    running.handle = handle;
    let from = handle.getSize();
    let res = null;
    let made = '';
    for (;;) {
      const headers = from > 0 ? { Range: `bytes=${from}-` } : {};
      res = await fetch(url, { headers, credentials: 'same-origin', signal: controller.signal, cache: 'no-store' });
      made = res.headers.get('x-offline-job') || '';
      if (made && job && made !== job && from > 0) {
        // The server made this MP4 again since (it restarted): what is
        // here belongs to the other one, so start over.
        try { await res.body.cancel(); } catch (e) { /* gone */ }
        handle.truncate(0);
        from = 0;
        job = made;
        postMessage({ type: 'job', id, job });
        continue;
      }
      break;
    }
    if (made && made !== job) { job = made; postMessage({ type: 'job', id, job }); }
    if (res.status === 416 && from > 0) {
      handle.flush();
      postMessage({ type: 'done', id, received: from, total: from });
      return;
    }
    if (res.status !== 200 && res.status !== 206) {
      let detail = '';
      try { detail = (await res.json()).detail || ''; } catch (e) { /* not JSON */ }
      postMessage({ type: 'failed', id, status: res.status, detail });
      return;
    }
    if (res.status === 200 && from > 0) {
      // The server sent the whole file: start the file over.
      handle.truncate(0);
      from = 0;
    }
    // Still being made by the server: no size yet, only about how big.
    const total = totalOf(res, from);
    const estimate = Number(res.headers.get('x-offline-estimate') || 0);
    const need = (total || estimate) - from;
    if (need > 0) {
      // Room for the rest? Say so plainly rather than fill the phone.
      try {
        const est = await navigator.storage.estimate();
        const free = (est.quota || 0) - (est.usage || 0);
        if (est.quota && free < need + (50 << 20)) {
          controller.abort();
          postMessage({ type: 'failed', id, fatal: true, reason: 'space', need, free });
          return;
        }
      } catch (e) { /* no estimate: go ahead */ }
    }
    postMessage({ type: 'progress', id, received: from, total, estimate });
    const reader = res.body.getReader();
    let at = from;
    let told = Date.now();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      handle.write(value, { at });
      at += value.byteLength;
      const now = Date.now();
      if (now - told > 500) {
        told = now;
        postMessage({ type: 'progress', id, received: at, total, estimate });
      }
    }
    handle.flush();
    let size = total;
    if (made) {
      // An MP4 the server made: its first bytes are only final once it is
      // finished (the size of its data), so take them again now.
      const head = await fetch(url, { headers: { Range: `bytes=0-${HEAD - 1}` }, credentials: 'same-origin',
                                      signal: controller.signal, cache: 'no-store' });
      const m = /\/(\d+)\s*$/.exec(head.headers.get('content-range') || '');
      if (head.status !== 206 || !m || (head.headers.get('x-offline-job') || made) !== made) {
        try { await head.body.cancel(); } catch (e) { /* gone */ }
        // Not finished yet (or made again): carry on from here.
        postMessage({ type: 'failed', id, received: at, reason: 'short' });
        return;
      }
      size = Number(m[1]);
      handle.write(new Uint8Array(await head.arrayBuffer()), { at: 0 });
      if (at > size) { handle.truncate(size); at = size; }
      handle.flush();
    }
    if (size && at < size) {
      postMessage({ type: 'failed', id, received: at, total: size, reason: 'short' });
      return;
    }
    postMessage({ type: 'done', id, received: at, total: size || at });
  } catch (e) {
    try { if (handle) handle.flush(); } catch (_) { /* closed */ }
    const size = (() => { try { return handle ? handle.getSize() : 0; } catch (_) { return 0; } })();
    postMessage({ type: 'failed', id, received: size, reason: controller.signal.aborted ? 'stopped' : 'network',
                  detail: String(e && e.name || e) });
  } finally {
    try { if (handle) handle.close(); } catch (_) { /* already */ }
    const then = running && running.id === id ? running.removeAfter : null;
    if (running && running.id === id) running = null;
    if (then) remove(then);
  }
}

async function remove(msg) {
  try {
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle('offline', { create: true });
    await dir.removeEntry(msg.file);
  } catch (e) { /* not there */ }
  postMessage({ type: 'removed', id: msg.id });
}

onmessage = (ev) => {
  const msg = ev.data || {};
  if (msg.cmd === 'start') {
    if (running) { postMessage({ type: 'failed', id: msg.id, reason: 'busy' }); return; }
    start(msg);
  } else if (msg.cmd === 'stop') {
    if (running && (!msg.id || running.id === msg.id)) running.controller.abort();
  } else if (msg.cmd === 'remove') {
    if (running && running.id === msg.id) {
      running.removeAfter = msg;     // once its file is closed
      running.controller.abort();
    } else {
      remove(msg);
    }
  }
};
