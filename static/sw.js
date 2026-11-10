// Service worker tối thiểu: giúp trình duyệt nhận diện là app có thể cài đặt.
// Dữ liệu động (/api/*) luôn lấy trực tiếp từ server, không cache.

const CACHE_NAME = "fkm-factory-shell-v11";
const SHELL_ASSETS = [
  "/",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    // Mở trang: ưu tiên mạng, mất kết nối thì dùng bản đã lưu
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put("/", copy));
          return response;
        })
        .catch(() => caches.match("/"))
    );
    return;
  }

  // Tài nguyên tĩnh: ưu tiên mạng, lỗi thì lấy từ cache
  event.respondWith(
    fetch(request).catch(() => caches.match(request))
  );
});
