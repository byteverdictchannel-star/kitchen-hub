// Keeps the page itself available when the internet is down.
// Strategy: try the network first, fall back to the saved copy.
const CACHE = "kitchen-hub-v11";
const SHELL = ["./", "index.html", "config.json", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return; // weather API: page handles it
  if (url.pathname.endsWith("news.json")) return; // page keeps its own copy
  e.respondWith(
    fetch(e.request, { cache: "no-cache" })  // always ask GitHub if there is a newer copy
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true }))
  );
});
