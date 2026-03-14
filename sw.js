const CACHE = 'deeeptalk-v1'
const OFFLINE_URL = '/offline'

// Assets to cache on install
const PRECACHE = [
  '/',
  'https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=DM+Mono:wght@400;500&display=swap',
  'https://cdn.socket.io/4.7.2/socket.io.min.js',
  'https://unpkg.com/vue@3/dist/vue.global.prod.js'
]

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(cache => {
      // Cache what we can, don't fail install if CDN is slow
      return Promise.allSettled(PRECACHE.map(url => cache.add(url).catch(() => {})))
    }).then(() => self.skipWaiting())
  )
})

self.addEventListener('activate', e => {
  // Delete old caches
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  )
})

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url)

  // Never intercept socket.io websocket/polling requests
  if (url.pathname.startsWith('/socket.io')) return

  // For navigation requests (page loads): try network, fall back to cache, then offline page
  if (e.request.mode === 'navigate') {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          // Cache the fresh page
          const clone = res.clone()
          caches.open(CACHE).then(c => c.put(e.request, clone))
          return res
        })
        .catch(() =>
          caches.match(e.request).then(cached => cached || offlinePage())
        )
    )
    return
  }

  // For other requests: cache-first (fonts, scripts)
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached
      return fetch(e.request).then(res => {
        if (res.ok) {
          const clone = res.clone()
          caches.open(CACHE).then(c => c.put(e.request, clone))
        }
        return res
      }).catch(() => new Response('', { status: 503 }))
    })
  )
})

function offlinePage() {
  return new Response(`
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>deeepTalk — Offline</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    height:100dvh;display:flex;flex-direction:column;
    align-items:center;justify-content:center;gap:16px;
    background:#0d0f14;color:#e8eaf0;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
    text-align:center;padding:24px;
  }
  .icon{font-size:64px;margin-bottom:8px}
  h1{font-size:24px;font-weight:600;color:#6c8fff}
  p{font-size:15px;color:#6b7280;max-width:280px;line-height:1.6}
  button{
    margin-top:8px;padding:12px 28px;
    background:linear-gradient(135deg,#6c8fff,#a78bfa);
    border:none;border-radius:12px;color:#fff;
    font-size:15px;font-weight:600;cursor:pointer;
  }
</style>
</head>
<body>
  <div class="icon">💬</div>
  <h1>deeepTalk</h1>
  <p>You're offline. Connect to the internet to chat with your friends.</p>
  <button onclick="location.reload()">Try Again</button>
</body>
</html>`, {
    headers: { 'Content-Type': 'text/html' }
  })
}
