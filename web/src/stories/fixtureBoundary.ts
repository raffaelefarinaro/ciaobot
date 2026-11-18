import { shallowReactive } from 'vue'

/** A declared story route: exact method and path (pathname + search + hash), answered in memory. */
export type FixtureRoute = {
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE' | 'HEAD'
  path: string
  respond: () => Response | Promise<never>
}

/**
 * An in-memory stand-in for one declared app socket. `sent` records what the
 * component wrote; the `server*` methods are the story's far end. Deliveries
 * and component sends never touch a native socket.
 */
export type FixtureSocket = {
  readonly url: string
  readyState: number
  bufferedAmount: number
  readonly sent: ReadonlyArray<string | ArrayBufferLike | ArrayBufferView | Blob>
  onopen: ((event: Event) => void) | null
  onmessage: ((event: MessageEvent) => void) | null
  onerror: ((event: Event) => void) | null
  onclose: ((event: CloseEvent) => void) | null
  addEventListener(type: string, listener: (event: Event | MessageEvent | CloseEvent) => void): void
  removeEventListener(type: string, listener: (event: Event | MessageEvent | CloseEvent) => void): void
  send(data: string | ArrayBufferLike | ArrayBufferView | Blob): void
  close(code?: number, reason?: string): void
  serverOpen(): void
  serverMessage(data: string | ArrayBufferLike | ArrayBufferView | Blob): void
  serverError(): void
  serverClose(code?: number, reason?: string): void
}

export type FixtureSocketScript = (socket: FixtureSocket) => void

export type FixtureConfig = {
  routes: FixtureRoute[]
  sockets?: Array<{ path: string, script: FixtureSocketScript }>
}

export type FixtureBoundary = {
  violations: string[]
  requests: string[]
  dispose: () => void
}

let active = false
const lifecycleEvents = new Set([
  'storyRenderPhaseChanged', 'storyRendered', 'storyFinished', 'storyUnmounted',
  'storyErrored', 'storyThrewException', 'storyMissing', 'currentStoryWasSet',
  'storybook/instrumenter/sync', 'previewKeydown', 'pong',
])

// The method names the vitest browser runner's page side calls over its
// control socket (@vitest/browser client.js, tester and orchestrator bundles,
// and vitest's runtime rpc: result reporting, console forwarding, module/mock
// resolution, user-event commands). Anything else is refused.
const runnerMethods = new Set([
  'onCollected', 'onQueued', 'onTaskUpdate', 'onTaskArtifactRecord', 'onAfterSuiteRun',
  'onUserConsoleLog', 'onUnhandledError', 'onAsyncLeaks', 'onOrchestratorReady', 'onCancel',
  'getCountOfFailedTests', 'sendLog', 'debug', 'resolveId', 'resolveMock', 'resolve',
  'registerMock', 'unregisterMock', 'clearMocks', 'invalidate', 'ensureModuleGraphEntry',
  'snapshotSaved', 'resolveSnapshotPath', 'triggerCommand',
])

// The runner's birpc envelopes are flatted-encoded: a root object whose string
// values index into the array (booleans stay inline). A request is
// `{m, a, t:'q'}` with an optional string `i` (absent for events) and an
// optional `o:true` (optional calls); m must be an allow-listed runner method.
// A response is `{t:'s', i, r|e}`.
function isRunnerEnvelope(data: unknown): boolean {
  if (typeof data !== 'string') return false
  try {
    const flat = JSON.parse(data) as unknown
    if (!Array.isArray(flat) || typeof flat[0] !== 'object' || flat[0] === null) return false
    const root = flat[0] as Record<string, unknown>
    if (!Object.keys(root).every(key => ['m', 'a', 't', 'i', 'r', 'e', 'o'].includes(key))) return false
    const field = (key: string) => typeof root[key] === 'string' ? flat[Number(root[key])] : undefined
    if ('i' in root && typeof field('i') !== 'string') return false
    if ('o' in root && root.o !== true) return false
    if (field('t') === 'q') {
      const method = field('m')
      return typeof method === 'string' && runnerMethods.has(method) && !('r' in root) && !('e' in root)
    }
    return field('t') === 's' && typeof field('i') === 'string' && !('m' in root) && !('o' in root)
  } catch { return false }
}

// Only established workshop sockets can reach this seam: the replaced
// constructor rejects everything except declared app sockets. Never forward
// custom/invoke/save/editor events.
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
    if (url.pathname === '/__vitest_browser_api__') {
      // The browser interaction runner's page-load control socket; it does not
      // exist outside `npm run test-storybook`. The exact query shape is pinned
      // so a repurposed path fails.
      return [...url.searchParams.keys()].sort().join(',') === 'method,projectName,rpcId,sessionId,token,type'
        && isRunnerEnvelope(data)
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

// GET/HEAD carry a request body only from a confused client, so those are
// violations even on a declared exact path. Writes are accepted on the write
// methods declared for them.
const readMethods = new Set(['GET', 'HEAD'])
const routeMethods = new Set([...readMethods, 'POST', 'PUT', 'PATCH', 'DELETE'])

const CONNECTING = 0
const OPEN = 1
const CLOSING = 2
const CLOSED = 3

// The shared prototype of every fake: assigning it as the host constructor's
// `prototype` keeps `instanceof WebSocket` true after the swap, and the
// ready-state constants remain reachable both statically and per instance.
const fixtureSocketProto = Object.assign(Object.create(Object.prototype), {
  CONNECTING, OPEN, CLOSING, CLOSED,
})

function frameSize(data: string | ArrayBufferLike | ArrayBufferView | Blob): number {
  if (typeof data === 'string') return data.length
  if (typeof ArrayBuffer !== 'undefined' && data instanceof ArrayBuffer) return data.byteLength
  if (typeof Blob !== 'undefined' && data instanceof Blob) return data.size
  return (data as ArrayBufferView).byteLength
}

/**
 * The fake is a plain object on a shared prototype (so `instanceof WebSocket`
 * holds once the constructor is replaced). The story's `script` drives the
 * far end: driver deliveries are queued and flushed on the next microtask, so
 * component handlers assigned synchronously after `new WebSocket` receive
 * them, exactly as a real server's frames arrive after the call returns.
 */
function createFixtureSocket(
  script: FixtureSocketScript,
  url: URL,
  reject: (transport: string, detail?: string) => never,
): FixtureSocket {
  const listeners = new Map<string, Set<(event: Event | MessageEvent | CloseEvent) => void>>()
  const sent: Array<string | ArrayBufferLike | ArrayBufferView | Blob> = []
  const queue: Array<[string, Event | MessageEvent | CloseEvent]> = []
  let state = CONNECTING
  let bufferedAmount = 0
  let scheduled = false
  function fire(type: string, event: Event | MessageEvent | CloseEvent) {
    const property = `on${type}` as 'onopen' | 'onmessage' | 'onerror' | 'onclose'
    const handler = fake[property]
    if (handler) handler(event as never)
    for (const listener of listeners.get(type) ?? []) listener(event)
  }
  function deliver(type: string, event: Event | MessageEvent | CloseEvent) {
    if (state === CLOSED) return
    queue.push([type, event])
    if (scheduled) return
    scheduled = true
    queueMicrotask(() => {
      scheduled = false
      for (const [type2, event2] of queue.splice(0)) fire(type2, event2)
    })
  }
  // Native closing semantics, simplified: close() ends the handshake; code
  // 1005 is the spec's "no status received" for a clean close without one.
  // The close event is delivered through the CLOSING window so deliveries
  // queued afterwards are still dropped.
  function shut(code?: number, reason?: string) {
    if (state === CLOSED) return
    state = CLOSING
    deliver('close', new CloseEvent('close', { code: code ?? 1005, reason: reason ?? '' }))
    state = CLOSED
  }
  const fake = Object.setPrototypeOf({
    url: url.toString(),
    get readyState() { return state },
    get bufferedAmount() { return bufferedAmount },
    get sent() { return [...sent] },
    onopen: null,
    onmessage: null,
    onerror: null,
    onclose: null,
    addEventListener(type: string, listener: (event: Event | MessageEvent | CloseEvent) => void) {
      const set = listeners.get(type) ?? new Set()
      set.add(listener)
      listeners.set(type, set)
    },
    removeEventListener(type: string, listener: (event: Event | MessageEvent | CloseEvent) => void) {
      listeners.get(type)?.delete(listener)
    },
    send(data: string | ArrayBufferLike | ArrayBufferView | Blob) {
      if (state !== OPEN) throw new Error(`Fixture socket send in readyState ${state}`)
      sent.push(data)
      bufferedAmount += frameSize(data)
    },
    close(code?: number, reason?: string) { shut(code, reason) },
    serverOpen() {
      if (state !== CONNECTING) return
      state = OPEN
      deliver('open', new Event('open'))
    },
    serverMessage(data: string | ArrayBufferLike | ArrayBufferView | Blob) {
      if (state !== OPEN) throw new Error('Fixture socket serverMessage before serverOpen')
      deliver('message', new MessageEvent('message', { data }))
    },
    serverError() {
      if (state === CLOSED) return
      deliver('error', new Event('error'))
    },
    serverClose(code?: number, reason?: string) { shut(code, reason) },
  }, fixtureSocketProto)
  // The script acts as the far end; a faulty script is a story bug and must
  // surface as a failure of the construction, never a half-open fake.
  try {
    script(fake as FixtureSocket)
  } catch (error) {
    return reject('WebSocket', `${url.href} — socket script failed: ${String(error)}`)
  }
  return fake as FixtureSocket
}

/**
 * A trusted-component fixture boundary, not a sandbox for hostile JavaScript.
 * No native fetch fallback, including "same-origin" or broad asset prefixes.
 * Storybook modules/assets and tightly scoped existing workshop socket messages
 * are infrastructure, not API fixtures. No app bootstrap runs here.
 */
export function installFixtureBoundary(config: FixtureConfig): FixtureBoundary {
  if (active) throw new Error('Fixture boundary already mounted; inline/concurrent docs are forbidden')
  if (!Array.isArray(config.routes)
    || config.routes.some(route => !route || typeof route.path !== 'string' || !route.path.startsWith('/')
      || typeof route.method !== 'string' || !routeMethods.has(route.method)
      || typeof route.respond !== 'function')) {
    throw new TypeError('installFixtureBoundary routes must each declare method, absolute path and respond()')
  }
  const sockets = config.sockets ?? []
  if (!Array.isArray(sockets) || sockets.some(socket => !socket || typeof socket.path !== 'string'
    || !socket.path.startsWith('/') || typeof socket.script !== 'function')) {
    throw new TypeError('installFixtureBoundary sockets must each declare an absolute path and script()')
  }
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
    held.forEach(rejectHeld => rejectHeld(new Error('Fixture disposed')))
    held.clear()
    restore.reverse().forEach(fn => fn())
    active = false
  }
  function settle(response: Response | Promise<never>): Response | Promise<Response> {
    if (response instanceof Response) return response
    // A respond() that never resolves holds the request until teardown, as
    // native loading would; dispose settles it with the disposal error.
    const gate = new Promise<Response>((_resolve, rejectHeld) => { held.add(rejectHeld) })
    return Promise.race([response, gate])
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
    const route = url.origin === location.origin && !url.username && !url.password
      ? config.routes.find(candidate => candidate.method === method && candidate.path === path)
      : undefined
    if (!route || body != null && readMethods.has(method)) {
      return reject('fetch', `${method} ${url.href}`)
    }
    requests.push(path)
    try {
      // Not awaited: a never-resolving respond() promise is held until
      // teardown settles it, exactly like native loading would be.
      return settle(route.respond())
    } catch (error) {
      return reject('fetch', `${method} ${url.href} — responder failed: ${String(error)}`)
    }
  }
  function declaredSocket(input?: string | URL) {
    let url: URL | undefined
    try { url = new URL(String(input)) } catch { url = undefined }
    const path = url ? `${url.pathname}${url.search}` : ''
    const socket = url && url.origin === location.origin.replace(/^http/, 'ws')
      ? sockets.find(candidate => candidate.path === path)
      : undefined
    if (!url || !socket) return reject('WebSocket', url ? url.href : String(input))
    return createFixtureSocket(socket.script, url, reject)
  }
  try {
    replace(globalThis, 'fetch', fixtureFetch)
    // Also fence instances created before installation, not only constructors.
    for (const key of ['open', 'send'] as const) {
      replace(XMLHttpRequest.prototype, key, function () { return reject(`XMLHttpRequest.${key}`) })
    }
    if (typeof WebSocket !== 'undefined') {
      // Established native sockets (workshop infrastructure) stay fenced.
      const nativeSend = WebSocket.prototype.send
      replace(WebSocket.prototype, 'send', function (this: WebSocket, data: unknown) {
        if (infrastructureMessage(this, data)) return nativeSend.call(this, data as string)
        return reject('WebSocket.send')
      })
      replace(WebSocket.prototype, 'constructor', function () { return reject('WebSocket') })
      // New constructions are refused unless the URL is a declared app socket:
      // same-origin, exact path. A declared socket gets the in-memory fake.
      const host = declaredSocket
      host.prototype = fixtureSocketProto
      Object.assign(host, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 })
      replace(globalThis, 'WebSocket', host)
    }
    for (const key of ['XMLHttpRequest', 'EventSource'] as const) {
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
