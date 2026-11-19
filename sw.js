// 离线可用的 service worker。
// 关键：页面本身（HTML）也必须 network-first —— 否则部署了新页面，老用户还一直看缓存里的旧页面，
// 而数据文件已经换成新的拆分格式，就会出现「就业 0 条」这种旧壳配新数据的事故。
const CACHE = 'xjtu-info-v3';
const SHELL = ['./', './index.html', './manifest.webmanifest', './icon-192.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

function networkFirst(req) {
  return fetch(req, { cache: 'no-store' }).then((r) => {
    if (r && r.status === 200) {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(req, copy));
    }
    return r;
  }).catch(() => caches.match(req).then((hit) => hit || Response.error()));
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;

  // 页面与数据一律先网络：宁可慢一点，也不能让人看旧页面配新数据
  const isDoc = req.mode === 'navigate' || url.pathname.endsWith('/') || url.pathname.endsWith('.html');
  const isData = /\/feed[^/]*\.json$/.test(url.pathname) || url.pathname.endsWith('.ics');
  if (isDoc || isData) {
    e.respondWith(networkFirst(req));
    return;
  }

  // 图标等静态资源：先缓存，后台再更新
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
