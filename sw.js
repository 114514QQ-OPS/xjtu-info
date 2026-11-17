// 离线可用的最小 service worker：
// 壳（HTML / 图标 / manifest）用 cache-first，数据 feed.json 用 network-first。
// 这样断网时还能看到上一次的通知，联网后拿到新的。
const CACHE = 'xjtu-info-v2';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // 数据：先网络，失败再拿缓存（保证看到的是最新一次成功抓取）
  if (url.pathname.endsWith('/feed.json')) {
    e.respondWith(
      fetch(req).then((r) => {
        const copy = r.clone();
        caches.open(CACHE).then((c) => c.put(req, copy));
        return r;
      }).catch(() => caches.match(req))
    );
    return;
  }

  // 壳：先缓存，后台再更新
  e.respondWith(
    caches.match(req).then((hit) => {
      const net = fetch(req).then((r) => {
        if (r && r.status === 200) {
          const copy = r.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return r;
      }).catch(() => hit);
      return hit || net;
    })
  );
});

