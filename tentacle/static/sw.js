/* Tentacle service worker — app-shell caching for installable PWA + fast loads.
 *
 * Strategy:
 *   - API / dynamic / SSE routes: never intercepted (always live network).
 *   - Navigations ("/"): network-first with cached shell fallback (offline UI).
 *   - /static/* assets: cache-first (URLs are version-busted, so safe).
 *   - The Offline page's videos are not here: they are files in the
 *     browser's own storage (OPFS), played from there (offline.js).
 *   - Save offline with the page closed: a push message says a title is
 *     ready on the server (services/offline_saves.py); tapping it opens the
 *     Offline page, which in Chrome hands the copy to Background Fetch (only
 *     a page may start one). When the browser has all of it, it is stored
 *     here as the page would (OPFS, else the 'tentacle-offline-files' cache)
 *     and its record in IndexedDB marked saved, page open or not.
 * Bump CACHE to force all clients to re-fetch the shell after a deploy.
 */
const CACHE = 'tentacle-shell-v7';
const SHELL = [
  '/',
  // The Offline page's download worker (static/js/offline.js); the page's
  // own scripts are read from it at install (precache)
  '/static/js/offline-worker.js?v=3',
  '/static/site.webmanifest',
  '/static/brand/mark-small.svg',
  '/static/web-app-manifest-192x192.png',
  '/static/web-app-manifest-512x512.png',
];

// The page and every script and stylesheet it loads (their ?v= as the page
// names them now): the app opens with no internet even if it was opened only
// once since this worker came (a page's first load isn't served through it).
async function precache() {
  const c = await caches.open(CACHE);
  await c.addAll(SHELL).catch(() => {});
  try {
    const res = await fetch('/', { cache: 'no-store' });
    if (!res.ok) return;
    const html = await res.clone().text();
    await c.put('/', res);
    const urls = new Set([...html.matchAll(/(?:src|href)="(\/static\/[^"]+)"/g)].map((m) => m[1]));
    await Promise.all([...urls].map((u) => c.add(u).catch(() => {})));
  } catch (e) { /* offline while installing: the shell list is there */ }
}

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(precache());
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== OFF_FILES).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  let url;
  try { url = new URL(req.url); } catch (e) { return; }

  // Same-origin only; leave cross-origin (fonts, images) to the network.
  if (url.origin !== self.location.origin) return;

  // Never cache dynamic data — always hit the network.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.startsWith('/hdhr/') ||
    url.pathname.startsWith('/Tentacle') ||
    url.pathname === '/sw.js'
  ) return;

  // App-shell navigations: network-first, fall back to cached shell offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put('/', copy)).catch(() => {});
          return res;
        })
        .catch(() => caches.match('/').then((r) => r || caches.match(req)))
    );
    return;
  }

  // Static assets: cache-first, populate cache on first fetch.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then((cached) =>
        cached ||
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
      )
    );
  }
});

// ── Save offline with the page closed ─────────────────────────────────────

const OFF_FILES = 'tentacle-offline-files';

function offDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('tentacle-offline', 1);
    req.onupgradeneeded = () => {
      if (!req.result.objectStoreNames.contains('titles')) req.result.createObjectStore('titles', { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function offRecord(id, change) {
  const db = await offDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('titles', 'readwrite');
    const store = tx.objectStore('titles');
    const get = store.get(id);
    let rec = null;
    get.onsuccess = () => {
      rec = get.result || null;
      if (rec && change) { Object.assign(rec, change); store.put(rec); }
    };
    tx.oncomplete = () => resolve(rec);
    tx.onerror = () => reject(tx.error);
  });
}

async function offTellPages() {
  const all = await self.clients.matchAll({ includeUncontrolled: true, type: 'window' });
  all.forEach((c) => c.postMessage({ type: 'offline-changed' }));
}

self.addEventListener('push', (event) => {
  let msg = {};
  try { msg = event.data ? event.data.json() : {}; } catch (e) { msg = {}; }
  event.waitUntil((async () => {
    const save = msg.save || null;
    if (save && save.id) {
      // Ready on the server: this device's record (if it asked) goes on to its
      // copy, which the page starts when it opens (Chrome lets only a page
      // start a Background Fetch; it then goes on with the page closed).
      const rec = await offRecord(save.id).catch(() => null);
      if (rec && rec.state === 'preparing' && (rec.device || 'pc') === save.device) {
        await offRecord(save.id, { state: 'waiting', size: save.size || rec.size || 0, server_fraction: 1 });
      }
    }
    await offTellPages();
    await self.registration.showNotification(msg.title || 'Tentacle', {
      body: msg.body || '',
      tag: msg.tag || undefined,
      icon: '/static/web-app-manifest-192x192.png',
      badge: '/static/web-app-manifest-192x192.png',
      data: { url: msg.url || '/#offline' },
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = new URL((event.notification.data && event.notification.data.url) || '/#offline', self.location.origin).href;
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = all.find((c) => new URL(c.url).origin === self.location.origin);
    if (open) {
      await open.focus();
      try { await open.navigate(url); } catch (e) { /* not ours to move */ }
      return;
    }
    await self.clients.openWindow(url);
  })());
});

// Background Fetch has the whole file: stored as the page stores one.
self.addEventListener('backgroundfetchsuccess', (event) => {
  const bg = event.registration;
  const id = bg.id.replace(/^off-/, '');
  event.waitUntil((async () => {
    const rec = await offRecord(id).catch(() => null);
    const records = await bg.matchAll();
    const res = records.length ? await records[0].responseReady : null;
    if (!rec || !res || !res.ok) {
      if (rec) await offRecord(id, { state: 'waiting', received: 0, bg_failed: true });
      await offTellPages();
      return;
    }
    const file = `${rec.id}.${(rec.container || 'mp4').replace(/[^a-z0-9]/gi, '') || 'mp4'}`;
    let where = 'opfs';
    let size = Number(res.headers.get('content-length') || 0) || bg.downloaded;
    try {
      const root = await navigator.storage.getDirectory();
      const dir = await root.getDirectoryHandle('offline', { create: true });
      const fh = await dir.getFileHandle(file, { create: true });
      if (typeof fh.createWritable !== 'function') throw new Error('no writable here');
      const w = await fh.createWritable();
      await res.clone().body.pipeTo(w);
      size = (await fh.getFile()).size;
    } catch (e) {
      // This browser can't write the file from here: kept in a cache instead
      where = 'cache';
      await (await caches.open(OFF_FILES)).put(`/offline-file/${id}`, res);
    }
    await offRecord(id, { state: 'done', received: size, size, where, error: '' });
    // The device has it: the server's copy may go
    try {
      await fetch(`/api/offline/saves/${id}?device=${encodeURIComponent(rec.device || 'pc')}&can=${encodeURIComponent(rec.can || '')}`,
        { method: 'DELETE', credentials: 'same-origin' });
    } catch (e) { /* offline: it lets go of it later by itself */ }
    const name = rec.kind === 'episode' && rec.series ? `${rec.series} S${rec.season}E${rec.episode}` : rec.name;
    try { await event.updateUI({ title: `${name} is saved` }); } catch (e) { /* no UI */ }
    await offTellPages();
  })());
});

self.addEventListener('backgroundfetchfail', (event) => {
  const id = event.registration.id.replace(/^off-/, '');
  // The page carries on the usual way, from the start
  event.waitUntil(offRecord(id, { state: 'waiting', received: 0, bg_failed: true }).then(offTellPages).catch(() => {}));
});

self.addEventListener('backgroundfetchabort', (event) => {
  const id = event.registration.id.replace(/^off-/, '');
  event.waitUntil(offRecord(id).then((rec) => rec && rec.state === 'bgfetch'
    ? offRecord(id, { state: 'waiting', received: 0, bg_failed: true }) : null).then(offTellPages).catch(() => {}));
});

self.addEventListener('backgroundfetchclick', (event) => {
  event.waitUntil(self.clients.openWindow('/#offline'));
});
