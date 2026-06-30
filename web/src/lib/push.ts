import { api } from './api'

function urlBase64ToUint8Array(b64: string): ArrayBuffer {
  const padding = '='.repeat((4 - (b64.length % 4)) % 4)
  const raw = atob((b64 + padding).replace(/-/g, '+').replace(/_/g, '/'))
  const buf = new ArrayBuffer(raw.length)
  const view = new Uint8Array(buf)
  for (let i = 0; i < raw.length; i++) view[i] = raw.charCodeAt(i)
  return buf
}

export function pushSupported(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window
}

async function getRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator)) return null
  return (await navigator.serviceWorker.getRegistration()) || (await navigator.serviceWorker.ready)
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  const reg = await getRegistration()
  if (!reg) return null
  return await reg.pushManager.getSubscription()
}

export async function isPushEnabled(): Promise<boolean> {
  if (!pushSupported()) return false
  const sub = await currentSubscription()
  return Boolean(sub)
}

export async function enablePush(): Promise<void> {
  if (!pushSupported()) throw new Error('Push not supported in this browser')
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error('Notification permission denied')
  const reg = await getRegistration()
  if (!reg) throw new Error('Service worker not registered')
  const { public_key } = await api.get<{ public_key: string }>('/api/push/public-key')
  if (!public_key) throw new Error('Server missing VAPID key')
  const existing = await reg.pushManager.getSubscription()
  if (existing) await existing.unsubscribe()
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(public_key),
  })
  await api.post('/api/push/subscribe', { subscription: sub.toJSON() })
}

export async function disablePush(): Promise<void> {
  const sub = await currentSubscription()
  if (!sub) return
  try {
    await api.post('/api/push/unsubscribe', { endpoint: sub.endpoint })
  } catch { /* ignore */ }
  await sub.unsubscribe()
}
