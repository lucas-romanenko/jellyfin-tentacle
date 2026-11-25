// Tentacle Offline: downloads one title's video into this device's own
// storage (OPFS), a piece at a time, never holding the film in memory.
//
// Started by offline.js with {cmd: 'start', id, url, file}. It writes what
// arrives at the byte it belongs to (a sync access handle, only possible in a
// worker) and, after an interruption, asks for the rest from the byte the
// file already has (Range), so nothing is fetched twice. It reports
// {type: 'progress' | 'done' | 'failed', ...}; offline.js decides whether to
// try again (a lost connection, a busy server) or give up (no permission).
'use strict';

let running = null;   // {id, controller, handle}

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
    const headers = from > 0 ? { Range: `bytes=${from}-` } : {};
    const res = await fetch(url, { headers, credentials: 'same-origin', signal: controller.signal, cache: 'no-store' });
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
    const total = totalOf(res, from);
    if (total) {
      // Room for the rest? Say so plainly rather than fill the phone.
      try {
        const est = await navigator.storage.estimate();
        const free = (est.quota || 0) - (est.usage || 0);
        if (est.quota && free < (total - from) + (50 << 20)) {
          controller.abort();
          postMessage({ type: 'failed', id, fatal: true, reason: 'space', need: total - from, free });
          return;
        }
      } catch (e) { /* no estimate: go ahead */ }
    }
    postMessage({ type: 'progress', id, received: from, total });
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
        postMessage({ type: 'progress', id, received: at, total });
      }
    }
    handle.flush();
    if (total && at < total) {
      postMessage({ type: 'failed', id, received: at, total, reason: 'short' });
      return;
    }
    postMessage({ type: 'done', id, received: at, total: total || at });
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
