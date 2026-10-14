// Caches the icons only. Pages always come from the network, so prices are never stale.
const CACHE = "dealwatch-static-v1";
const ASSETS = ["/static/icon-192.png", "/static/icon-512.png", "/apple-touch-icon.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.origin === location.origin && (ASSETS.includes(url.pathname))) {
    e.respondWith(caches.match(e.request).then((hit) => hit || fetch(e.request)));
  }
});
