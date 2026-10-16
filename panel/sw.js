// Service worker for the aria2 panel: caches the app shell so the panel opens
// offline and loads instantly. Dynamic data (/api/, /d/) is never cached.
const CACHE = 'panel-v1';
const SHELL = ['/', '/app.css', '/app.js', '/manifest.webmanifest', '/icon.svg'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()).catch(() => {}));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/d/')) return;
  const isNav = req.mode === 'navigate';
  const key = isNav ? '/' : url.pathname;
  if (!isNav && !SHELL.includes(key)) return;
  // Network-first so updates land immediately; fall back to cache when offline.
  e.respondWith(
    fetch(req).then(res => {
      if (res && res.ok) {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(key, copy)).catch(() => {});
      }
      return res;
    }).catch(() => caches.match(key).then(m => m || caches.match('/')))
  );
});
