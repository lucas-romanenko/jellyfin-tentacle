/* Tentacle service worker — app-shell caching for installable PWA + fast loads.
 *
 * Strategy:
 *   - API / dynamic / SSE routes: never intercepted (always live network).
 *   - Navigations ("/"): network-first with cached shell fallback (offline UI).
 *   - /static/* assets: cache-first (URLs are version-busted, so safe).
 * Bump CACHE to force all clients to re-fetch the shell after a deploy.
 */
const CACHE = 'tentacle-shell-v1';
const SHELL = [
  '/',
  '/static/site.webmanifest',
  '/static/favicon.svg',
  '/static/web-app-manifest-192x192.png',
  '/static/web-app-manifest-512x512.png',
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {}))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
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
