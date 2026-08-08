self.addEventListener("install", () => self.skipWaiting());

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Authenticated pages, terminal traffic, and file responses are deliberately
// never cached. The service worker only enables standalone PWA installation.
