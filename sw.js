/* Service worker for the installable student app.
   - App shell: network-first, falls back to the last cached copy (opens offline).
   - /api/user-data: network-first, falls back to the last cached snapshot.
   - 401/403 responses evict cached user-data and are never cached; no non-OK response is cached.
   - Other API calls are never cached. */
const VERSION = 'v3';
const SHELL_CACHE = 'shell-' + VERSION;
const DATA_CACHE = 'data-' + VERSION;
const FONT_CACHE = 'fonts-' + VERSION;
const SHELL_URLS = ['/webapp', '/manifest.webmanifest', '/logo.png', '/icons/icon-192.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) =>
        Promise.allSettled(
          SHELL_URLS.map(async (assetUrl) => {
            const res = await fetch(assetUrl);
            if (res && res.ok) {
              await cache.put(assetUrl, res.clone());
            }
          })
        )
      )
      .catch(() => {})
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => ![SHELL_CACHE, DATA_CACHE, FONT_CACHE].includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('message', (event) => {
  if (event.data === 'clear-data') {
    event.waitUntil(
      caches.keys()
        .then((keys) => Promise.all(keys.filter((k) => k.startsWith('data-')).map((k) => caches.delete(k))))
        .catch(() => {})
    );
  }
});

async function networkFirst(request, cacheName, cacheKey) {
  const cache = await caches.open(cacheName);
  const key = cacheKey || request;
  try {
    const res = await fetch(request);
    if (res && (res.status === 401 || res.status === 403)) {
      await cache.delete(key);
      return res;
    }
    if (res && res.ok) {
      await cache.put(key, res.clone());
    }
    return res;
  } catch (err) {
    const cached = await cache.match(key);
    if (cached) return cached;
    throw err;
  }
}

async function cacheFirstStatic(request, cacheKey) {
  const cache = await caches.open(SHELL_CACHE);
  const key = cacheKey || request;
  const hit = (await cache.match(key)) || (await cache.match(request));
  if (hit) return hit;
  const res = await fetch(request);
  if (res && res.ok) {
    await cache.put(key, res.clone());
  }
  return res;
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);

  if (url.origin === self.location.origin) {
    if (url.pathname === '/api/user-data') {
      // Key without the cache-busting query so one snapshot is kept.
      event.respondWith(networkFirst(req, DATA_CACHE, '/api/user-data'));
      return;
    }
    if (url.pathname.startsWith('/api/')) return;
    if (url.pathname === '/webapp' || url.pathname === '/app') {
      event.respondWith(networkFirst(req, SHELL_CACHE, '/webapp'));
      return;
    }
    if (url.pathname === '/logo.png' || url.pathname.startsWith('/icons/') || url.pathname === '/manifest.webmanifest') {
      event.respondWith(cacheFirstStatic(req, url.pathname));
    }
    return;
  }

  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(
      caches.open(FONT_CACHE).then((cache) =>
        cache.match(req).then((hit) => {
          const net = fetch(req).then((res) => {
            if (res && res.ok) {
              cache.put(req, res.clone());
            }
            return res;
          }).catch(() => hit);
          return hit || net;
        })
      )
    );
  }
});
