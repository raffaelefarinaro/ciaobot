const CACHE_NAME = 'ciaobot-v0.5.2'
const STATIC_ASSETS = ['/', '/index.html', '/manifest.json']
const ICON = '/icons/icon-192.png'
const BADGE = '/icons/icon-192.png'
const UNREAD_CACHE = 'ciaobot-unread-v0.5.2'
const UNREAD_KEY = '/__unread__'

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
  )
  self.skipWaiting()
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== UNREAD_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  )
  self.clients.claim()
})

// --- Unread badge state -----------------------------------------------------
// Stored in a Cache entry as JSON: { [chat_id]: count }. Cache survives SW
// restarts. We update navigator.setAppBadge with the total.

async function readUnread() {
  try {
    const cache = await caches.open(UNREAD_CACHE)
    const res = await cache.match(UNREAD_KEY)
    if (!res) return {}
    return await res.json()
  } catch {
    return {}
  }
}

async function writeUnread(state) {
  const cache = await caches.open(UNREAD_CACHE)
  await cache.put(
    UNREAD_KEY,
    new Response(JSON.stringify(state), {
      headers: { 'Content-Type': 'application/json' },
    })
  )
}

async function updateBadge(state) {
  let total = 0
  for (const k of Object.keys(state)) total += state[k] || 0
  try {
    if (total > 0 && self.navigator.setAppBadge) {
      await self.navigator.setAppBadge(total)
    } else if (self.navigator.clearAppBadge) {
      await self.navigator.clearAppBadge()
    }
  } catch {
    // setAppBadge unsupported; ignore.
  }
  return total
}

async function incUnread(chatId) {
  const state = await readUnread()
  const key = chatId || '_'
  state[key] = (state[key] || 0) + 1
  await writeUnread(state)
  await updateBadge(state)
}

async function clearUnread(chatId) {
  const state = await readUnread()
  if (chatId) {
    delete state[chatId]
  } else {
    for (const k of Object.keys(state)) delete state[k]
  }
  await writeUnread(state)
  await updateBadge(state)
}

// Replace the SW unread cache wholesale with the page's view of truth.
// Used by the page to reconcile drift after suspend/resume, cross-device
// reads, or swipe-dismissed notifications that don't fire notificationclick.
async function setUnread(state) {
  const cleaned = {}
  for (const k of Object.keys(state || {})) {
    const v = Number(state[k]) || 0
    if (v > 0) cleaned[k] = v
  }
  await writeUnread(cleaned)
  await updateBadge(cleaned)
}

// --- Pending notification target (iOS cold-start fallback) ------------------
// When notificationclick doesn't fire on iOS, the PWA opens to start_url.
// We store the target chat here so the frontend can check on boot/resume.
const PENDING_TARGET_KEY = '/__pending_target__'

async function setPendingTarget(chatId) {
  try {
    const cache = await caches.open(UNREAD_CACHE)
    await cache.put(
      PENDING_TARGET_KEY,
      new Response(JSON.stringify({ chat_id: chatId, ts: Date.now() }), {
        headers: { 'Content-Type': 'application/json' },
      })
    )
  } catch { /* ignore */ }
}

async function getPendingTarget() {
  try {
    const cache = await caches.open(UNREAD_CACHE)
    const res = await cache.match(PENDING_TARGET_KEY)
    if (!res) return null
    const data = await res.json()
    // Stale after 5 minutes
    if (Date.now() - (data.ts || 0) > 5 * 60 * 1000) return null
    return data.chat_id || null
  } catch {
    return null
  }
}

async function clearPendingTarget() {
  try {
    const cache = await caches.open(UNREAD_CACHE)
    await cache.delete(PENDING_TARGET_KEY)
  } catch { /* ignore */ }
}

// --- Push -------------------------------------------------------------------

self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = { title: 'ciaobot', body: event.data ? event.data.text() : '' }
  }
  const title = data.title || 'ciaobot'
  const chatId = data.chat_id || ''
  const options = {
    body: data.body || '',
    icon: ICON,
    badge: BADGE,
    tag: chatId || 'ciaobot',
    renotify: true,
    vibrate: [120, 60, 120],
    data: { chat_id: chatId },
    actions: [
      { action: 'open', title: 'open' },
      { action: 'dismiss', title: 'dismiss' },
    ],
  }
  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, options),
      incUnread(chatId),
      setPendingTarget(chatId),
    ])
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const chatId = event.notification.data?.chat_id || ''
  if (event.action === 'dismiss') {
    event.waitUntil(
      (async () => {
        await clearUnread(chatId)
        await clearPendingTarget()
      })()
    )
    return
  }
  const url = chatId ? `/chat/${encodeURIComponent(chatId)}` : '/'
  event.waitUntil((async () => {
    await clearUnread(chatId)
    await clearPendingTarget()
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of clients) {
      if ('focus' in client) {
        await client.focus()
        // Prefer direct navigation (Chrome/Android); fall back to postMessage.
        if ('navigate' in client) {
          try {
            await client.navigate(url)
            return
          } catch {
            // navigate may throw on some clients; fall through to postMessage
          }
        }
        if (chatId && 'postMessage' in client) {
          client.postMessage({ type: 'open-chat', chat_id: chatId })
        }
        return
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(url)
  })())
})

// Page tells us a chat is now in focus -> clear its badge.
self.addEventListener('message', (event) => {
  const msg = event.data || {}
  if (msg.type === 'chat-focused') {
    event.waitUntil(clearUnread(msg.chat_id || ''))
  } else if (msg.type === 'clear-badge') {
    event.waitUntil(clearUnread(''))
  } else if (msg.type === 'sync-unread') {
    // Wholesale reconciliation: page sends the authoritative unread map
    // (one entry per unread chat). Overwrites any stale push-incremented
    // counts the SW accumulated while the page was closed/suspended.
    event.waitUntil(setUnread(msg.state || {}))
  } else if (msg.type === 'get-pending-target') {
    event.waitUntil((async () => {
      const target = await getPendingTarget()
      const source = event.source
      if (target && source && 'postMessage' in source) {
        source.postMessage({ type: 'pending-target', chat_id: target })
      }
    })())
  }
})

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return
  if (event.request.method !== 'GET') return
  if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) return
  event.respondWith(
    fetch(event.request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone()
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone))
        }
        return response
      })
      .catch(async () => {
        // SPA navigations (e.g. /chat/<id>) aren't cached under their own URL
        // — only the app shell is. When the network is momentarily
        // unavailable (server restart, flaky connection), fall back to the
        // cached shell so client-side routing still loads the page, instead
        // of returning a hard network error (blank/broken chat).
        if (event.request.mode === 'navigate') {
          const shell =
            (await caches.match('/index.html')) || (await caches.match('/'))
          if (shell) return shell
        }
        const cached = await caches.match(event.request)
        return cached || Response.error()
      })
  )
})
