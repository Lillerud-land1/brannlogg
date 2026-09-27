// Brannlogg Stord: hentar alltid nyaste versjon når nettet er der,
// og viser siste lagra versjon når telefonen er utan nett.
const LAGER = "brannlogg-v1";

self.addEventListener("install", e => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  e.respondWith(
    fetch(e.request, {cache: "no-store"})
      .then(svar => {
        const kopi = svar.clone();
        caches.open(LAGER).then(c => c.put(e.request, kopi));
        return svar;
      })
      .catch(() => caches.match(e.request).then(m => m || caches.match("./")))
  );
});
