/**
 * Service worker for Collaborative Checklists.
 *
 * The page shell is static — it contains no user data — so unlike the previous
 * version it is safe to cache. That is what makes an offline start possible:
 * the app opens, renders from the last cached state, and shows which changes
 * are waiting to be sent.
 *
 * /api/ and /auth/ are never cached or served from cache. Those responses are
 * per-user, and serving one person's checklists to the next person to use the
 * browser is exactly the bug a cache-first rule would introduce.
 *
 * Bump VERSION when the logic in this file changes. Asset edits do not need a
 * bump: their URLs carry a content hash, and a successful navigation refreshes
 * the cached shell, so the new URLs are picked up on their own.
 */

const VERSION = "v3";
const CACHE = `checklist-${VERSION}`;

// Fetched at install. The shell's own <link> and <script> URLs are discovered
// from its HTML, because they carry content hashes and cannot be listed here.
const BASE_ASSETS = [
  "/offline.html",
  "/manifest.json",
  "/static/app.css",
  "/static/icon-192.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(precache().then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((name) => name.startsWith("checklist-") && name !== CACHE)
            .map((name) => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

async function precache() {
  const cache = await caches.open(CACHE);

  const response = await fetch("/", { cache: "no-cache" });
  const html = await response.clone().text();
  await cache.put("/", response);

  const assets = new Set(BASE_ASSETS);
  for (const match of html.matchAll(/(?:href|src)="(\/static\/[^"]+)"/g)) {
    assets.add(match[1]);
  }
  // One missing optional asset must not abort the install: allSettled keeps
  // the worker usable and leaves the failure visible in the console.
  await Promise.allSettled([...assets].map((asset) => cache.add(asset)));
}

self.addEventListener("fetch", (event) => {
  const request = event.request;

  if (request.method !== "GET") return;

  let url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return;
  }

  // Same-origin only. A future third-party asset stays network-only rather
  // than being cached somewhere it cannot be validated.
  if (url.origin !== self.location.origin) return;

  // Authenticated traffic: never cached, never served from cache.
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/auth/")) {
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(networkFirstShell(request));
    return;
  }

  // The manifest lives at a fixed URL and changes when the app is deployed, so
  // a cache-first rule would keep serving an old copy. Revalidate it, and fall
  // back to the cached copy only when the network is unavailable.
  if (url.pathname === "/manifest.json") {
    event.respondWith(networkFirstAsset(request));
    return;
  }

  if (url.pathname.startsWith("/static/")) {
    event.respondWith(cacheFirst(request));
  }
});

async function networkFirstShell(request) {
  try {
    const response = await fetch(request);
    // A well-formed 502/521 from a proxy is not a usable page, so treat any
    // server-side error the same as a connection failure.
    if (response && response.status < 500) {
      if (new URL(request.url).pathname === "/") {
        // Keep the offline copy current, so an asset change does not wait for
        // a VERSION bump to reach a device that is offline next time.
        const cache = await caches.open(CACHE);
        cache.put("/", response.clone());
      }
      return response;
    }
  } catch (error) {
    /* fall through to the cache */
  }

  const cached = await caches.match("/");
  if (cached) return cached;

  const offline = await caches.match("/offline.html");
  if (offline) return offline;

  return new Response("Offline", {
    status: 503,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  if (response && response.ok) {
    cache.put(request, response.clone());
  }
  return response;
}

async function networkFirstAsset(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    // A proxy error is not a usable manifest, so keep the last good copy.
    if (response && response.status < 500) {
      cache.put(request, response.clone());
      return response;
    }
  } catch (error) {
    /* fall through to the cache */
  }

  const cached = await cache.match(request);
  if (cached) return cached;
  return fetch(request);
}
