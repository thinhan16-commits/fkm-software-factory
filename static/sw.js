// FKM Software Factory - Service Worker v32
// Tự động giải phóng cache cũ, luôn ưu tiên tải giao diện mới nhất từ server

const CACHE_NAME = "fkm-factory-shell-v33";
const SHELL_ASSETS = [
  "/",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .catch((err) => console.warn("[SW] Cache preload skipped:", err))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => {
          console.log("[SW] Deleting old cache:", key);
          return caches.delete(key);
        })
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data === "SKIP_WAITING") {
    self.skipWaiting();
  }
  if (event.data === "PURGE_CACHE") {
    caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k))));
  }
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    // Luôn ưu tiên tải HTML mới nhất từ server (no-store), rớt mạng hoàn toàn mới dùng cache
    event.respondWith(
      fetch(request, { cache: "no-store" })
        .then((response) => {
          if (response && response.status === 200) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
          }
          return response;
        })
        .catch(() => caches.match("/") || caches.match(request))
    );
    return;
  }

  // Tài nguyên tĩnh: ưu tiên mạng, lỗi mạng thì lấy từ cache
  event.respondWith(
    fetch(request, { cache: "no-store" })
      .then((response) => {
        if (response && response.status === 200 && (url.pathname.endsWith(".png") || url.pathname.endsWith(".webmanifest"))) {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        }
        return response;
      })
      .catch(() => caches.match(request))
  );
});
