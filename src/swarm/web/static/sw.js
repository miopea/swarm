// v23 BUMPED TO EVICT A LEAK, not merely to refresh the shell.
// v22 accumulated one permanent Cache Storage entry per /api/health?_=<timestamp>
// poll — a unique URL every few seconds, cached by the old catch-all fetch handler and
// never evicted. That grew the BROWSER process to 12.3GB and killed the tab with a
// Chromium OOM (0xE0000008, 2MB allocation).
// The activate handler below deletes every cache whose name is not CACHE_NAME, so
// renaming is what actually reclaims the gigabytes already banked in v22 — the fetch-
// handler fix only stops new ones. Together with skipWaiting()/clients.claim() the
// eviction happens on the next load, with no manual "clear site data" step.
const CACHE_NAME = 'swarm-v23';
const APP_SHELL = ['/manifest.json', '/static/theme.js', '/static/bees/happy.svg', '/static/icon-192.png', '/static/icon-512.png', '/offline.html'];

const INLINE_OFFLINE = `<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swarm — Offline</title>
<script src="/static/theme.js"></script>
<style>:root,[data-theme="dark"]{--bg:#15130F;--text:#F5F1E8;--accent:#F1B83D;--muted:#B8AE9F}[data-theme="light"]{--bg:#F6F4EF;--text:#211D18;--accent:#7A5000;--muted:#665E53}
body{background:var(--bg);color:var(--text);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
h1{color:var(--accent);font-size:1.4rem}p{color:var(--muted);font-size:.9rem;margin:.5rem 0}</style></head>
<body><div><h1>Waiting for Swarm...</h1><p>The server should restart automatically.</p><p>Retrying...</p>
<script>setInterval(function(){fetch('/api/health').then(function(r){if(r.ok)location.replace('/')}).catch(function(){})},3000)</script>
</div></body></html>`;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// One-shot flag: skip the navigate race timeout for the next request
let _skipRace = false;

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'skip-race') _skipRace = true;
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Never cache WebSocket upgrades, API, action, or partial responses
  if (url.pathname.startsWith('/action/') || url.pathname.startsWith('/ws')
      || url.pathname.startsWith('/api/') || url.pathname.startsWith('/partials/')) return;

  if (req.mode === 'navigate') {
    // If dashboard.js pre-fetched successfully, skip the race timeout
    if (_skipRace) {
      _skipRace = false;
      e.respondWith(
        fetch(req).then(resp => {
          return resp;
        }).catch(() =>
          caches.match('/offline.html').then(cached =>
            cached || new Response(INLINE_OFFLINE, {
              status: 503,
              headers: { 'Content-Type': 'text/html' }
            })
          )
        )
      );
      return;
    }
    // Race fetch against a 2s timeout to avoid blank page flash
    e.respondWith(
      Promise.race([
        fetch(req),
        new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 2000))
      ]).catch(() =>
        caches.match('/offline.html').then(cached =>
          cached || new Response(INLINE_OFFLINE, {
            status: 503,
            headers: { 'Content-Type': 'text/html' }
          })
        )
      )
    );
    return;
  }

  // Operator does rapid daemon reloads (os.execv'd from the Reload
  // button). During the ~1s window the daemon is restarting, network
  // fetches fail; if we fall back to cache for JS/CSS the browser
  // ends up running the OLD code even though the daemon now serves
  // new code. The layout fixes don't take effect until SHIFT+CTRL+F5
  // bypasses the SW. For dynamic-code assets, do network-only — let
  // the request fail so a normal reload picks up fresh code.
  const dynamic = url.pathname.endsWith('.js') || url.pathname.endsWith('.css') || url.pathname.endsWith('.html');
  if (dynamic) {
    // The tiny pre-paint theme controller is part of the offline shell. Keep
    // network-first freshness, but fall back to its versioned cache while the
    // daemon is restarting so the offline page still honors the user's mode.
    if (url.pathname === '/static/theme.js') {
      e.respondWith(fetch(req).catch(() => caches.match('/static/theme.js')));
    } else {
      e.respondWith(fetch(req));
    }
    return;
  }

  // THE MEMORY LEAK (operator: browser climbing to 12-14GB with all extensions
  // disabled, only Swarm open).
  //
  // This branch used to cache EVERY response, keyed by URL. That is bounded only while
  // the URLs are. They are not: the dashboard polls
  //     /api/health?_=<Date.now()>
  // which is a UNIQUE URL on every call, so each poll wrote a permanent new Cache
  // Storage entry that nothing ever evicted.
  //
  // Cache Storage lives in the BROWSER process, which is exactly why this was so hard
  // to see: the renderer stayed at 116MB, page reloads never freed it (the cache
  // outlives the page), only a full browser exit reclaimed it, and heap/DOM/socket
  // counters all read flat throughout.
  //
  // API responses have no business in a durable cache anyway — they are live state, and
  // serving a stale one from cache would be its own bug. Only same-origin GETs for
  // static assets are cached now; everything else goes straight to the network, falling
  // back to any cache entry only when offline.
  const cacheUrl = new URL(req.url);
  const cacheable = req.method === 'GET'
    && cacheUrl.origin === self.location.origin
    && cacheUrl.pathname.startsWith('/static/')
    && !cacheUrl.searchParams.has('_');

  if (!cacheable) {
    e.respondWith(fetch(req).catch(() => caches.match(req)));
    return;
  }

  e.respondWith(
    fetch(req).then(resp => {
      const clone = resp.clone();
      caches.open(CACHE_NAME).then(c => c.put(req, clone));
      return resp;
    }).catch(() => caches.match(req))
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(clients => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          return client.focus();
        }
      }
      return self.clients.openWindow('/');
    })
  );
});
