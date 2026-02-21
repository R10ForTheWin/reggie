// Reggie service worker — minimal, just enables PWA installability
self.addEventListener('install',  e => self.skipWaiting());
self.addEventListener('activate', e => clients.claim());
self.addEventListener('fetch',    e => e.respondWith(fetch(e.request)));
