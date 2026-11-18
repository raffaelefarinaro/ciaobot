import { shallowReactive } from 'vue'
import { STATS_RANGES } from '../lib/usageStats'
import type { StatsRange } from '../lib/usageStats'
import { declaredUsagePath, syntheticUsageRaw, usageWorkspaces } from './usageFixtures'

export type UsageFixtureState = 'loaded' | 'initial-loading' | 'failed' | 'refresh-loading'
export { usageWorkspaces }

const paths = usageWorkspaces.flatMap(workspace => STATS_RANGES.map(range => declaredUsagePath(workspace, range)))

let active = false
const lifecycleEvents = new Set([
  'storyRenderPhaseChanged', 'storyRendered', 'storyFinished', 'storyUnmounted',
  'storyErrored', 'storyThrewException', 'storyMissing', 'currentStoryWasSet',
  'storybook/instrumenter/sync', 'previewKeydown', 'pong',
])

const runnerMethods = new Set([
  'onCollected', 'onQueued', 'onTaskUpdate', 'onTaskArtifactRecord', 'onAfterSuiteRun',
  'onUserConsoleLog', 'onUnhandledError', 'onAsyncLeaks', 'onOrchestratorReady', 'onCancel',
  'getCountOfFailedTests', 'sendLog', 'debug', 'resolveId', 'resolveMock', 'resolve',
  'registerMock', 'unregisterMock', 'clearMocks', 'invalidate', 'ensureModuleGraphEntry',
  'snapshotSaved', 'resolveSnapshotPath', 'triggerCommand',
])

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

function parseDeclaredPath(path: string): { workspace: string; range: StatsRange } | null {
  if (!paths.includes(path)) return null
  const qs = path.split('?', 2)[1] ?? ''
  const params = new URLSearchParams(qs)
  const workspace = params.get('workspace') ?? ''
  const range = Number(params.get('range'))
  if (!usageWorkspaces.includes(workspace as (typeof usageWorkspaces)[number])) return null
  if (!STATS_RANGES.includes(range as StatsRange)) return null
  return { workspace, range: range as StatsRange }
}

/** Fail-closed GET fixture for the Usage page's stats client. */
export function installUsageBoundary(state: UsageFixtureState) {
  if (active) throw new Error('Connected usage story already mounted; inline/concurrent docs are forbidden')
  active = true
  const violations = shallowReactive<string[]>([])
  const requests = shallowReactive<string[]>([])
  const restore: Array<() => void> = []
  const held = new Set<(reason: Error) => void>()
  let disposed = false
  let answered = 0
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
    held.forEach(rejectHeld => rejectHeld(new Error('Usage fixture disposed')))
    held.clear()
    restore.reverse().forEach(fn => fn())
    active = false
  }
  function hold(): Promise<Response> {
    return new Promise<Response>((_resolve, rejectHeld) => { held.add(rejectHeld) })
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
    const declared = parseDeclaredPath(path)
    if (url.origin !== location.origin || url.username || url.password || method !== 'GET' || body != null || !declared) {
      return reject('fetch', `${method} ${url.href}`)
    }
    requests.push(path)
    answered += 1
    if (state === 'initial-loading') return hold()
    if (state === 'failed') {
      return new Response(JSON.stringify({ error: 'Synthetic usage unavailable' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (state === 'refresh-loading' && answered > 1) return hold()
    const payload = syntheticUsageRaw(declared.workspace, declared.range)
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }
  try {
    replace(globalThis, 'fetch', fixtureFetch)
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