/**
 * Service worker for the Collaborative Checklist app.
 *
 * Scope note: this deliberately does NOT cache the HTML document. The page is
 * rendered server-side with the signed-in user inlined
 * ({{ user | tojson }}), so a cached copy would leave one person's name and
 * email sitting in Cache Storage on a shared device, readable without
 * authenticating. Offline you get the shell's assets and a clear offline page
 * instead of a stale copy of somebody's checklist.
 *
 * That still satisfies what a PWA is for in practice: an installable app with a
 * home-screen icon and standalone (chrome-less) display.
 *
 * Bump VERSION when you change anything here. Old caches are deleted on
 * activate, and the browser revalidates this file on each load, so an updated
 * worker replaces the old one automatically.
 */

const VERSION = 'v2';
const ASSET_CACHE = `checklist-assets-${VERSION}`;
const CDN_HOSTS = ['cdn.tailwindcss.com', 'cdn.jsdelivr.net'];

// Static, public, and free of any user's data.
const PRECACHE = [
  '/manifest.json',
  '/static/icon-192.png',
  '/static/icon-512.png',
];

const OFFLINE_PAGE = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Offline</title>
<style>
  body{font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;padding:1.5rem}
  .card{text-align:center;max-width:22rem}
  h1{font-size:1.05rem;font-weight:600;margin:0 0 .5rem}
  p{font-size:.8rem;line-height:1.5;color:#9ca3af;margin:0}
  button{margin-top:1.25rem;padding:.5rem 1rem;font-size:.75rem;border:0;border-radius:.5rem;
         background:#2563eb;color:#fff;cursor:pointer}
</style></head>
<body><div class="card">
  <h1>You are offline</h1>
  <p>Checklists are stored on the server, so they need a connection to load.
     Your tasks are safe and will appear once you are back online.</p>
  <button onclick="location.reload()">Try again</button>
</div></body></html>`;

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(ASSET_CACHE)
      .then((cache) => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name.startsWith('checklist-assets-') && name !== ASSET_CACHE)
          .map((name) => caches.delete(name))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;

  // Writes and anything else that is not a plain GET is left alone.
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (err) {
    return;
  }

  if (url.origin !== self.location.origin && !CDN_HOSTS.includes(url.hostname)) return;

  // Authenticated traffic is never cached or served from cache. This is the
  // line that must not move: /api responses are per-user, so caching them
  // would show one person's checklists to the next person to use the browser,
  // and would keep serving them after logout.
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/auth/')) return;

  // Navigations go to the network first so a deploy is picked up immediately,
  // falling back to a neutral offline page rather than a cached document.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          // Catching only a thrown error is not enough. Behind a reverse proxy
          // or CDN, a downed origin comes back as a well-formed 502/521 rather
          // than a connection failure, so fetch resolves and the raw proxy error
          // would be shown. Treat any server-side error as offline.
          if (response && response.status >= 500) return offlineResponse();
          return response;
        })
        .catch(() => offlineResponse())
    );
    return;
  }

  // Static files and the CDN scripts the styling depends on. Cache first: the
  // icons never change, and the Tailwind CDN is what generates all the CSS, so
  // having it cached is what makes offline launch look right.
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json'
      || CDN_HOSTS.includes(url.hostname)) {
    event.respondWith(cacheFirst(request));
  }
});

function offlineResponse() {
  return new Response(OFFLINE_PAGE, {
    status: 503,
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
}

async function cacheFirst(request) {
  const cache = await caches.open(ASSET_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  const response = await fetch(request);
  // Opaque covers cross-origin responses fetched without CORS, which have a
  // zero status and cannot be inspected.
  if (response && (response.ok || response.type === 'opaque')) {
    cache.put(request, response.clone());
  }
  return response;
}
