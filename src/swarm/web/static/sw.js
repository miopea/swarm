// KILL SWITCH — deliberately a no-op service worker.
//
// The operator's Edge browser PROCESS climbed past 5GB at 4.3% sustained CPU and "very
// high" power, with ONLY Swarm open, while the Swarm renderer sat at 264MB and 0% CPU.
// The page is not looping; something between the page and the network/storage layer is,
// and the service worker is the only Swarm component that lives there — it intercepts
// every request and writes to Cache Storage.
//
// Rather than guess at which part of it misbehaves, this removes it entirely:
//   - unregisters itself, so the browser stops routing requests through it
//   - deletes every cache it ever created, reclaiming what was banked
//   - claims existing clients so it takes effect without a second reload
//
// COST: the PWA loses offline support and app-shell precaching. Nothing else — the app
// is fully server-rendered and does not depend on the worker to function.
//
// This is a DIAGNOSTIC as much as a fix. If browser-process memory and CPU return to
// normal, the service worker is confirmed and the full implementation (kept in git
// history at the previous release) can be restored piece by piece. If they do not, the
// worker is exonerated outright and the search moves elsewhere — which is worth more
// than a seventh speculative patch.

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    for (const key of await caches.keys()) {
      await caches.delete(key);
    }
    await self.registration.unregister();
    for (const client of await self.clients.matchAll({ type: 'window' })) {
      try { client.navigate(client.url); } catch (_) { /* best effort */ }
    }
  })());
});

// No fetch handler at all: requests go straight to the network, exactly as they would
// with no service worker installed.
