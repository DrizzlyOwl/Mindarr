// Mindarr Service Worker — network-first caching strategy.
// GET requests always try the network first; the cache is only used as a
// fallback when the network is unavailable (offline) or the request fails.
const CACHE_VERSION = "mindarr-v1.5.0";
const APP_SHELL = [
  "/offline",
  "/manifest.json",
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_VERSION)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;

  // Only handle GET requests; let everything else (POST/PUT/DELETE) pass
  // straight through to the network untouched.
  if (request.method !== "GET") {
    return;
  }

  // Never intercept cross-origin requests (CDNs, third-party APIs).
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }

  event.respondWith(networkFirst(request));
});

async function networkFirst(request) {
  const cache = await caches.open(CACHE_VERSION);
  try {
    const networkResponse = await fetch(request);
    // Only cache successful, cacheable responses.
    if (networkResponse && networkResponse.ok) {
      cache.put(request, networkResponse.clone());
    }
    return networkResponse;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) {
      return cached;
    }
    if (request.mode === "navigate") {
      const offline = await cache.match("/offline");
      if (offline) {
        return offline;
      }
    }
    throw err;
  }
}
