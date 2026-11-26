/* Service worker for the installable student app.
   - App shell: network-first, falls back to the last cached copy (opens offline).
   - Static CSS/JS & icons: precached on install and served cache-first.
   - /api/user-data: network-first, falls back to the last cached snapshot.
   - 401/403 responses evict cached user-data and are never cached; no non-OK response is cached.
   - Other API calls are never cached. */
const VERSION = 'v5';
const SHELL_CACHE = 'shell-' + VERSION;
const DATA_CACHE = 'data-' + VERSION;
const FONT_CACHE = 'fonts-' + VERSION;
const SHELL_URLS = [
  '/webapp',
  '/manifest.webmanifest',
  '/logo.png',
  '/icons/icon-192.png',
  '/static/css/app.css',
  '/static/js/state.js',
  '/static/js/dates.js',
  '/static/js/api.js',
  '/static/js/render.js',
  '/static/js/actions.js'
];

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
    if (
      url.pathname === '/logo.png' ||
      url.pathname.startsWith('/icons/') ||
      url.pathname === '/manifest.webmanifest' ||
      url.pathname.startsWith('/static/')
    ) {
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

function _safePushUrl(raw) {
  if (typeof raw !== 'string') return '/webapp';
  const s = raw.trim();
  if (!s || /[\u0000-\u001F\u007F]/.test(s)) return '/webapp';
  // Allow same-origin relative paths starting with single '/' (reject protocol-relative '//')
  if (s.startsWith('/') && !s.startsWith('//')) return s;
  // Allow explicit https:// URLs only
  if (/^https:\/\//i.test(s)) {
    try {
      const parsed = new URL(s);
      if (parsed.protocol === 'https:') return parsed.href;
    } catch (_) {}
  }
  return '/webapp';
}

self.addEventListener('push', (event) => {
  let title = 'دفتر الطالب';
  let body = '';
  let url = '/webapp';
  let tag = 'mutah-lms';

  if (event && event.data) {
    let parsed = null;
    try {
      parsed = event.data.json();
    } catch (_) {
      try {
        const txt = event.data.text();
        if (typeof txt === 'string' && txt.trim()) {
          try {
            parsed = JSON.parse(txt);
          } catch (__) {
            body = txt.trim();
          }
        }
      } catch (__) {}
    }
    if (parsed && typeof parsed === 'object') {
      if (typeof parsed.title === 'string' && parsed.title.trim()) title = parsed.title.trim();
      if (typeof parsed.body === 'string') body = parsed.body;
      if (typeof parsed.url === 'string') url = _safePushUrl(parsed.url);
      if (typeof parsed.tag === 'string' && parsed.tag.trim()) tag = parsed.tag.trim();
    }
  }

  const options = {
    body,
    icon: '/logo.png',
    badge: '/logo.png',
    tag,
    data: { url: _safePushUrl(url) }
  };

  if (self.registration && typeof self.registration.showNotification === 'function') {
    event.waitUntil(self.registration.showNotification(title, options));
  }
});

self.addEventListener('notificationclick', (event) => {
  if (event && event.notification && typeof event.notification.close === 'function') {
    event.notification.close();
  }
  const rawUrl = event && event.notification && event.notification.data && event.notification.data.url;
  const targetUrl = _safePushUrl(rawUrl);
  const resolvedTarget = new URL(targetUrl, self.location.origin).href;

  event.waitUntil((async () => {
    if (self.clients && typeof self.clients.matchAll === 'function') {
      const windowClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
      for (const client of windowClients) {
        try {
          const clientOrigin = new URL(client.url).origin;
          const targetOrigin = new URL(resolvedTarget).origin;
          if (clientOrigin === targetOrigin && typeof client.focus === 'function') {
            if (typeof client.navigate === 'function' && client.url !== resolvedTarget) {
              try { await client.navigate(resolvedTarget); } catch (_) {}
            }
            return await client.focus();
          }
        } catch (_) {}
      }
    }
    if (self.clients && typeof self.clients.openWindow === 'function') {
      return await self.clients.openWindow(resolvedTarget);
    }
    return undefined;
  })());
});
