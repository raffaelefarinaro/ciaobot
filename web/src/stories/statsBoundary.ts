import { shallowReactive } from 'vue'

export type StatsState = 'default' | 'loading' | 'failed' | 'partial'
export const statsWorkspaces = ['storybook-alpha', 'storybook-beta'] as const
const paths = statsWorkspaces.map(workspace => `/api/stats?workspace=${workspace}&range=30`)
const today = {
  chats_opened: 12, messages_sent: 24, chats_archived: 2, memories_saved: 3,
  projects_worked_on: 2, tasks_completed: 1,
  avg_chats_opened_30d: 1.2, avg_messages_sent_30d: 8.4, avg_chats_archived_30d: 0.5,
  avg_memories_saved_30d: 0.4, avg_projects_worked_on_30d: 1, avg_tasks_completed_30d: 0.2,
}
let active = false
const lifecycleEvents = new Set([
  'storyRenderPhaseChanged', 'storyRendered', 'storyFinished', 'storyUnmounted',
  'storyErrored', 'storyThrewException', 'storyMissing', 'currentStoryWasSet',
  'storybook/instrumenter/sync', 'pong',
])

// Only established workshop sockets can reach this seam: constructors below
// reject all new connections. Never forward custom/invoke/save/editor events.
function infrastructureMessage(socket: WebSocket, data: unknown): boolean {
  if (typeof data !== 'string') return false
  try {
    const url = new URL(socket.url)
    const expectedOrigin = location.origin.replace(/^http/, 'ws')
    if (url.origin !== expectedOrigin) return false
    if (socket.protocol === 'vite-hmr') {
      return url.pathname === '/' && [...url.searchParams.keys()].every(key => key === 'token')
        && data === '{"type":"ping"}'
    }
    const token = (globalThis as typeof globalThis & { CHANNEL_OPTIONS?: { wsToken?: string } }).CHANNEL_OPTIONS?.wsToken
    if (!token || url.pathname !== '/storybook-server-channel'
      || url.searchParams.get('token') !== token || url.searchParams.get('page') !== 'preview'
      || [...url.searchParams.keys()].sort().join(',') !== 'page,token') return false
    const message: unknown = JSON.parse(data)
    return !!message && typeof message === 'object'
      && lifecycleEvents.has((message as { type: string }).type)
  } catch { return false }
}

/**
 * A trusted-component fixture boundary, not a sandbox for hostile JavaScript.
 * No native fetch fallback, including "same-origin" or broad asset prefixes.
 * Storybook modules/assets and tightly scoped existing workshop socket messages
 * are infrastructure, not API fixtures. No app bootstrap runs here.
 */
export function installStatsBoundary(state: StatsState) {
  if (active) throw new Error('Connected story already mounted; inline/concurrent docs are forbidden')
  active = true
  const violations = shallowReactive<string[]>([])
  const requests = shallowReactive<string[]>([])
  const restore: Array<() => void> = []
  const held = new Set<(reason: Error) => void>()
  let disposed = false
  function reject(transport: string, detail = ''): never {
    const message = `Story network violation: ${transport} ${detail}`.trim()
    violations.push(message)
    throw new Error(message)
  }
  function replace(target: object, key: PropertyKey, value: unknown) {
    const descriptor = Object.getOwnPropertyDescriptor(target, key)
    Object.defineProperty(target, key, { configurable: true, writable: true, value })
    restore.push(() => {
      if (descriptor) Object.defineProperty(target, key, descriptor)
      else Reflect.deleteProperty(target, key)
    })
  }
  function dispose() {
    if (disposed) return
    disposed = true
    held.forEach(rejectHeld => rejectHeld(new Error('Stats fixture disposed')))
    held.clear()
    restore.reverse().forEach(fn => fn())
    active = false
  }
  const fixtureFetch: typeof fetch = async (input, init) => {
    if (disposed) return reject('fetch', 'fixture disposed')
    let url: URL
    try {
      url = new URL(input instanceof Request ? input.url : String(input), location.origin)
    } catch { return reject('fetch', 'invalid URL') }
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
    const body = init?.body ?? (input instanceof Request ? input.body : null)
    const path = `${url.pathname}${url.search}${url.hash}`
    if (url.origin !== location.origin || url.username || url.password || method !== 'GET' || body != null || !paths.includes(path)) {
      return reject('fetch', `${method} ${url.href}`)
    }
    requests.push(path)
    if (state === 'loading') return new Promise<Response>((_resolve, rejectHeld) => { held.add(rejectHeld) })
    const payload = state === 'failed' ? { error: 'Synthetic stats unavailable' }
      : state === 'partial' ? { today: { chats_opened: 0, messages_sent: 'invalid', memories_saved: 3, avg_chats_opened_30d: 0.4 } }
        : { today: { ...today, chats_opened: path === paths[1] ? 4 : 12 } }
    return new Response(JSON.stringify(payload), {
      status: state === 'failed' ? 503 : 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  try {
    replace(globalThis, 'fetch', fixtureFetch)
    // Also fence instances created before installation, not only constructors.
    for (const key of ['open', 'send'] as const) {
      replace(XMLHttpRequest.prototype, key, function () { return reject(`XMLHttpRequest.${key}`) })
    }
    if (typeof WebSocket !== 'undefined') {
      const nativeSend = WebSocket.prototype.send
      replace(WebSocket.prototype, 'send', function (this: WebSocket, data: unknown) {
        if (infrastructureMessage(this, data)) return nativeSend.call(this, data as string)
        return reject('WebSocket.send')
      })
    }
    for (const key of ['XMLHttpRequest', 'WebSocket', 'EventSource'] as const) {
      const blocked = function () { return reject(key) }
      const original = globalThis[key]
      if (original) replace(original.prototype, 'constructor', blocked)
      replace(globalThis, key, blocked)
    }
    // Beacon is normally inherited; patch that entry too, not just the
    // navigator instance (Navigator.prototype.sendBeacon.call is also a sink).
    const navigatorPrototype = Object.getPrototypeOf(navigator)
    if (navigatorPrototype && 'sendBeacon' in navigatorPrototype) {
      replace(navigatorPrototype, 'sendBeacon', function () { return reject('sendBeacon') })
    }
    replace(navigator, 'sendBeacon', function () { return reject('sendBeacon') })
  } catch (error) {
    dispose()
    throw error
  }
  return { violations, requests, dispose }
}
