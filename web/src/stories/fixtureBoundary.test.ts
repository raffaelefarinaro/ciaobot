// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { createApp, nextTick } from 'vue'
import HomeTodayStats from '../components/HomeTodayStats.vue'
import { useProjectStore } from '../stores/projects'
import preview from '../../.storybook/preview'
import { activeFixtureBoundary, assertNoFixtureViolations, fenceStory, installFixtureBoundary, type FixtureRoute, type FixtureSocket } from './fixtureBoundary'
import { mountConnected } from './mountConnected'
import { statsRoutes, type StatsState, usageRoute } from './homeTodayStatsFixture'

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).reverse().forEach(cleanup => cleanup()); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const path = '/api/stats?workspace=storybook-alpha&range=30'
function wsOrigin(): string {
  return location.origin.replace(/^http/, 'ws')
}
async function settleQueue(): Promise<void> {
  await new Promise(resolve => setTimeout(resolve, 0))
}

describe('connected fixture transport boundary', () => {
  it('answers only exact declared synthetic GETs without native fetch', async () => {
    const native = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('native transport'))
    const boundary = installFixtureBoundary({ routes: statsRoutes('default') })
    cleanups.push(boundary.dispose)
    expect((await (await fetch(path)).json()).today.chats_opened).toBe(12)
    for (const url of [
      '/api/stats?workspace=personal&range=30', `${path}&extra=1`, `${path}&range=7`,
      path.replace('30', '7'), `${path}#fragment`, '/api/tasks',
      `https://example.com${path}`, '//example.com/api/stats', '/index.json', '/assets/unknown.js',
    ]) await expect(fetch(url)).rejects.toThrow('Story network violation')
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS']) {
      await expect(fetch(path, { method })).rejects.toThrow('Story network violation')
    }
    await expect(fetch(new Request(new URL(path, location.origin), { method: 'POST' }))).rejects.toThrow()
    await expect(fetch(path, { method: 'GET', body: 'write' })).rejects.toThrow()
    expect(native).not.toHaveBeenCalled()
    expect(boundary.violations.length).toBe(18)
  })

  it('blocks XHR including a pre-existing instance, sockets, streams and beacon before native transport', () => {
    const xhr = new XMLHttpRequest()
    const open = vi.spyOn(XMLHttpRequest.prototype, 'open')
    const send = vi.spyOn(XMLHttpRequest.prototype, 'send')
    const socket = vi.fn(function () {})
    const stream = vi.fn(function () {})
    const beacon = vi.fn()
    vi.stubGlobal('WebSocket', socket)
    vi.stubGlobal('EventSource', stream)
    Object.defineProperty(navigator, 'sendBeacon', { configurable: true, value: beacon })
    cleanups.push(() => { Reflect.deleteProperty(navigator, 'sendBeacon') })
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    for (const attempt of [
      () => xhr.open('GET', path), () => xhr.send(), () => new XMLHttpRequest(),
      () => new WebSocket('wss://example.com'), () => new EventSource('/api/events'),
      () => navigator.sendBeacon('/api/write', 'data'),
    ]) expect(attempt).toThrow('Story network violation')
    expect(open).not.toHaveBeenCalled()
    expect(send).not.toHaveBeenCalled()
    expect(socket).not.toHaveBeenCalled()
    expect(stream).not.toHaveBeenCalled()
    expect(beacon).not.toHaveBeenCalled()
    expect(boundary.violations).toHaveLength(6)
  })

  it('refuses overlap and restores descriptors; held requests settle on teardown', async () => {
    const fetchBefore = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
    const boundary = installFixtureBoundary({ routes: statsRoutes('loading') })
    expect(() => installFixtureBoundary({ routes: statsRoutes('default') })).toThrow('already mounted')
    const pending = fetch(path)
    const rejection = expect(pending).rejects.toThrow('disposed')
    boundary.dispose()
    boundary.dispose()
    await rejection
    expect(Object.getOwnPropertyDescriptor(globalThis, 'fetch')).toEqual(fetchBefore)
    const fresh = installFixtureBoundary({ routes: statsRoutes('default') })
    cleanups.push(fresh.dispose)
    expect(fresh.violations).toEqual([])
  })

  it('records even a captured fixture fetch after teardown rather than falling through to native', async () => {
    const native = vi.spyOn(globalThis, 'fetch')
    const boundary = installFixtureBoundary({ routes: [] })
    const captured = fetch
    boundary.dispose()
    await expect(captured(path)).rejects.toThrow('disposed')
    expect(boundary.violations).toHaveLength(1)
    expect(native).not.toHaveBeenCalled()
  })

  it('forwards preview keyboard reports only on the authorized workshop channel', () => {
    const nativeSend = vi.fn()
    const socket = vi.fn(function () {})
    socket.prototype.send = nativeSend
    vi.stubGlobal('WebSocket', socket)
    vi.stubGlobal('CHANNEL_OPTIONS', { wsToken: 'synthetic-workshop-token' })
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    const origin = wsOrigin()
    const preview = { url: `${origin}/storybook-server-channel?token=synthetic-workshop-token&page=preview`, protocol: '' }
    const keyboard = JSON.stringify({
      type: 'previewKeydown',
      args: [{ event: { key: 'Tab', code: 'Tab', keyCode: 9, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false } }],
      from: 'preview',
    })
    const send = socket.prototype.send
    send.call(preview, keyboard)
    expect(nativeSend).toHaveBeenCalledExactlyOnceWith(keyboard)
    expect(boundary.violations).toEqual([])
    for (const [target, data] of [
      [{ ...preview, url: preview.url.replace(origin, 'wss://example.com') }, keyboard],
      [{ ...preview, url: preview.url.replace('/storybook-server-channel', '/ws/events') }, keyboard],
      [{ ...preview, url: preview.url.replace('synthetic-workshop-token', 'wrong') }, keyboard],
      [{ ...preview, url: preview.url.replace('page=preview', 'page=manager') }, keyboard],
      [preview, '{"type":"custom","event":"previewKeydown"}'],
      [preview, '{"type":"saveStory"}'],
      [preview, '{"type":"undeclaredSocketMessage"}'],
    ] as const) expect(() => send.call(target, data)).toThrow('Story network violation')
    expect(nativeSend).toHaveBeenCalledTimes(1)
    expect(boundary.violations).toHaveLength(7)
  })

  it('permits only the runner control socket, and only its own birpc envelopes', () => {
    const nativeSend = vi.fn()
    const socket = vi.fn(function () {})
    socket.prototype.send = nativeSend
    vi.stubGlobal('WebSocket', socket)
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    const origin = wsOrigin()
    const runner = { url: `${origin}/__vitest_browser_api__?type=tester&rpcId=rpc-1&sessionId=session-1&projectName=&method=tasks&token=0` }
    const send = socket.prototype.send
    const request = '[{"m":"1","a":"2","t":"3","i":"4"},"onCollected",["5"],"q","synthetic-id","synthetic"]'
    const response = '[{"t":"1","i":"2","r":"3"},"s","synthetic-id",null]'
    // birpc events carry no `i`; optional calls add `o:true`.
    const event = '[{"m":"1","a":"2","t":"3"},"onUserConsoleLog",["4"],"q","synthetic"]'
    const optional = '[{"m":"1","a":"2","t":"3","i":"4","o":true},"onTaskUpdate",[],"q","synthetic-id"]'
    send.call(runner, request)
    send.call(runner, response)
    send.call(runner, event)
    send.call(runner, optional)
    expect(nativeSend).toHaveBeenCalledTimes(4)
    expect(boundary.violations).toEqual([])
    for (const [target, data] of [
      [{ ...runner, url: runner.url.replace(origin, 'wss://example.com') }, request],
      [{ ...runner, url: runner.url.replace('/__vitest_browser_api__', '/api/write') }, request],
      [{ ...runner, url: runner.url.replace('&token=0', '&page=preview') }, request],
      [{ ...runner, url: runner.url.replace('type=tester&', '') }, request],
      [runner, '{"type":"saveStory"}'],
      [runner, '[{"m":"1","t":"2","i":"3"},0,"q","id"]'],
      [runner, '[{"m":"1","t":"2","i":"3"},"onCollected","s","id"]'],
      [runner, '[{"m":"1","a":"2","t":"3","i":"4","x":"5"},"onCollected",[],"q","id","extra"]'],
      [runner, '[{"m":"1","a":"2","t":"3"},"saveStory",[],"q"]'],
      [runner, '[{"m":"1","a":"2","t":"3"},"__proto__",[],"q"]'],
      [runner, '[{"m":"1","a":"2","t":"3","o":false},"onCollected",[],"q"]'],
      [runner, '[{"m":"1","a":"2","t":"3","o":"4"},"onCollected",[],"q","yes"]'],
      [runner, '[{"m":"1","a":"2","t":"3","i":7},"onCollected",[],"q"]'],
      [runner, '[{"t":"1","r":"2"},"s",null]'],
    ] as const) expect(() => send.call(target, data)).toThrow('Story network violation')
    expect(nativeSend).toHaveBeenCalledTimes(4)
    expect(boundary.violations).toHaveLength(14)
  })

  it('permits only established same-origin workshop lifecycle/heartbeat sends, never custom or write events', () => {
    const nativeSend = vi.fn()
    const socket = vi.fn(function () {})
    socket.prototype.send = nativeSend
    vi.stubGlobal('WebSocket', socket)
    vi.stubGlobal('CHANNEL_OPTIONS', { wsToken: 'synthetic-workshop-token' })
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    const origin = wsOrigin()
    const preview = { url: `${origin}/storybook-server-channel?token=synthetic-workshop-token&page=preview`, protocol: '' }
    const send = socket.prototype.send
    send.call(preview, '{"type":"storyRendered","args":["synthetic-story"]}')
    send.call({ url: `${origin}/?token=synthetic-hmr-token`, protocol: 'vite-hmr' }, '{"type":"ping"}')
    // What the dev server's Docs and Interactions addons emit during a render.
    for (const type of ['storybook/docs/snippet-rendered', 'storybook/actions/action-event', 'storybook/instrumenter/call', 'playFunctionThrewException']) {
      send.call(preview, JSON.stringify({ type, args: [] }))
    }
    expect(nativeSend).toHaveBeenCalledTimes(6)
    for (const [target, data] of [
      [preview, '{"type":"custom","event":"saveStory"}'],
      [preview, '{"type":"saveStory"}'],
      [{ ...preview, url: preview.url.replace('preview', 'manager') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace('synthetic-workshop-token', 'wrong') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace('/storybook-server-channel', '/api/write') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace(origin, 'wss://example.com') }, '{"type":"storyRendered"}'],
      [{ url: `${origin}/?token=synthetic-hmr-token`, protocol: 'vite-hmr' }, '{"type":"custom"}'],
    ] as const) expect(() => send.call(target, data)).toThrow('Story network violation')
    expect(nativeSend).toHaveBeenCalledTimes(6)
    expect(boundary.violations).toHaveLength(7)
  })

  it('mounts declared app sockets as in-memory fakes driven by the story script, never native', async () => {
    const native = globalThis.WebSocket
    const driven: FixtureSocket[] = []
    const boundary = installFixtureBoundary({
      routes: [],
      sockets: [{
        path: '/ws/chat/storybook-alpha',
        script: socket => {
          driven.push(socket)
          socket.serverOpen()
          socket.serverMessage('{"type":"keepalive"}')
        },
      }, {
        path: '/ws/chat/storybook-quiet',
        script: socket => { driven.push(socket); socket.serverMessage('too early') },
      }],
    })
    cleanups.push(boundary.dispose)
    const received: string[] = []
    const opens: number[] = []
    const closes: Array<{ code: number, reason: string }> = []
    const fake = new WebSocket(`${wsOrigin()}/ws/chat/storybook-alpha`)
    // The driver ran synchronously; state flips are synchronous, deliveries are
    // queued for the next microtask, after the component assigned handlers.
    expect(driven).toHaveLength(1)
    expect(driven[0]).toBe(fake)
    expect(fake).toBeInstanceOf(WebSocket)
    expect(fake).not.toBeInstanceOf(native)
    expect(fake.readyState).toBe(WebSocket.OPEN)
    fake.onopen = () => opens.push(fake.readyState)
    fake.onmessage = event => received.push(String((event as MessageEvent).data))
    fake.onclose = event => closes.push({ code: (event as CloseEvent).code, reason: (event as CloseEvent).reason })
    await settleQueue()
    expect(opens).toEqual([WebSocket.OPEN])
    expect(received).toEqual(['{"type":"keepalive"}'])
    expect(closes).toEqual([])
    fake.send('hello')
    expect(driven[0].sent).toEqual(['hello'])
    expect(driven[0].bufferedAmount).toBe(5)
    driven[0].serverMessage('{"type":"ok"}')
    await settleQueue()
    expect(received).toEqual(['{"type":"keepalive"}', '{"type":"ok"}'])
    fake.close(1000, 'done')
    await settleQueue()
    expect(fake.readyState).toBe(WebSocket.CLOSED)
    expect(closes).toEqual([{ code: 1000, reason: 'done' }])
    expect(() => fake.send('late')).not.toThrow()
    expect(driven[0].sent).toEqual(['hello'])
    driven[0].serverMessage('late server frame')
    driven[0].serverClose(1001)
    await settleQueue()
    expect(closes).toEqual([{ code: 1000, reason: 'done' }])
    // A declared path with any deviation keeps failing closed, and a script
    // that misuses the fake fails constructing instead of half-opening.
    for (const bad of [
      `${wsOrigin()}/ws/events`,
      `${wsOrigin()}/ws/chat/storybook-alpha?extra=1`,
      'wss://example.com/ws/chat/storybook-alpha',
      '/ws/chat/storybook-alpha',
    ]) expect(() => new WebSocket(bad)).toThrow('Story network violation')
    // `/ws/events` is app infrastructure in the real engine: never auto-declared.
    expect(() => new WebSocket(`${wsOrigin()}/ws/chat/storybook-quiet`))
      .toThrow('socket script failed: Error: Fixture socket serverMessage before serverOpen')
    expect(boundary.violations).toHaveLength(5)
    expect(driven).toHaveLength(2)
  })

  it('refuses the undeclared /ws/events socket outright, with no fake and no native attempt', () => {
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    expect(() => new WebSocket(`${wsOrigin()}/ws/events`)).toThrow('Story network violation')
    expect(() => new WebSocket(`${wsOrigin()}/ws/events?type=none`)).toThrow('Story network violation')
    expect(boundary.violations).toHaveLength(2)
    expect(boundary.requests).toEqual([])
  })
})

async function mount(state: StatsState, declared = true) {
  const host = document.createElement('div')
  document.body.append(host)
  const boundary = installFixtureBoundary({ routes: statsRoutes(state) })
  const fixture = mountConnected(host, {
    component: HomeTodayStats,
    routerRoutes: [usageRoute()],
    seed: pinia => { useProjectStore(pinia).activeWorkspace = declared ? 'storybook-alpha' : 'undeclared' },
  })
  cleanups.push(() => { fixture.dispose(); boundary.dispose(); host.remove() })
  await fixture.router.isReady()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
  return { ...fixture, host }
}

describe('real connected owner', () => {
  it('uses normalization, memory navigation, fresh store and workspace-scoped fixtures', async () => {
    const first = await mount('default')
    expect(first.host.querySelector('strong')?.textContent).toBe('12')
    const projects = useProjectStore(first.pinia)
    projects.activeWorkspace = 'storybook-beta'
    await nextTick()
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(first.host.querySelector('strong')?.textContent).toBe('4')
    const usageLink = first.host.querySelector('a') as HTMLAnchorElement
    usageLink.click()
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(first.router.currentRoute.value.path).toBe('/stats')
    expect(first.host.textContent).toContain('Inert Usage destination')
    expect(first.boundary.violations).toEqual([])
    first.dispose()
    first.boundary.dispose()
    const second = await mount('default')
    expect(second.pinia).not.toBe(first.pinia)
    expect(useProjectStore(second.pinia).activeWorkspace).toBe('storybook-alpha')
    expect(second.router.currentRoute.value.path).toBe('/')
    expect(second.host.querySelector('strong')?.textContent).toBe('12')
  })

  it.each(['loading', 'failed'] as const)('%s keeps the real owner’s five dashes without invented status UI', async state => {
    const fixture = await mount(state)
    expect([...fixture.host.querySelectorAll('strong')].map(el => el.textContent)).toEqual(Array(5).fill('—'))
    expect(fixture.host.querySelectorAll('.home-today-avg')).toHaveLength(0)
    expect(fixture.boundary.requests).toEqual([path])
    expect(fixture.boundary.violations).toEqual([])
  })

  it('normalizes missing and invalid fields, keeping valid zeroes and averages', async () => {
    const fixture = await mount('partial')
    expect([...fixture.host.querySelectorAll('strong')].map(el => el.textContent)).toEqual(['0', '—', '—', '3', '—'])
    expect(fixture.host.querySelector('.home-today-avg')?.textContent).toBe('avg 0.4')
  })

  it('exposes violations even when HomeTodayStats catches the rejection', async () => {
    const fixture = await mount('default', false)
    expect(fixture.host.querySelector('strong')?.textContent).toBe('—')
    expect(fixture.boundary.violations).toHaveLength(1)
    expect(fixture.boundary.violations[0]).toContain('undeclared')
  })
})

const writeRoute = (method: FixtureRoute['method'], path = '/api/things'): FixtureRoute => ({
  method, path, respond: () => new Response('{"ok":true}', { status: 201 }),
})

describe('boundary configuration', () => {
  it.each([
    ['routes not an array', { routes: 'x' }],
    ['route without respond', { routes: [{ method: 'GET', path: '/a' }] }],
    ['relative path', { routes: [{ method: 'GET', path: 'a', respond: () => new Response() }] }],
    ['unknown method', { routes: [{ method: 'TRACE', path: '/a', respond: () => new Response() }] }],
    ['null route', { routes: [null] }],
    ['socket without script', { routes: [], sockets: [{ path: '/ws/a' }] }],
    ['relative socket path', { routes: [], sockets: [{ path: 'ws/a', script: () => {} }] }],
    ['sockets not an array', { routes: [], sockets: 'x' }],
  ])('rejects %s without installing anything', (_name, config) => {
    const before = Object.getOwnPropertyDescriptor(globalThis, 'fetch')
    expect(() => installFixtureBoundary(config as never)).toThrow(TypeError)
    expect(Object.getOwnPropertyDescriptor(globalThis, 'fetch')).toEqual(before)
    expect(activeFixtureBoundary()).toBeUndefined()
  })

  it('rejects duplicate routes and duplicate socket paths', () => {
    expect(() => installFixtureBoundary({ routes: [writeRoute('POST'), writeRoute('POST')] })).toThrow('duplicate route POST /api/things')
    expect(() => installFixtureBoundary({
      routes: [],
      sockets: [{ path: '/ws/a', script: () => {} }, { path: '/ws/a', script: () => {} }],
    })).toThrow('duplicate socket path')
    // The same path under two methods is two routes, not a duplicate.
    const boundary = installFixtureBoundary({ routes: [writeRoute('POST'), writeRoute('GET')] })
    boundary.dispose()
  })
})

describe('declared write routes', () => {
  it.each(['POST', 'PUT', 'PATCH', 'DELETE'] as const)('accepts %s only when declared', async method => {
    const boundary = installFixtureBoundary({ routes: [writeRoute(method)] })
    cleanups.push(boundary.dispose)
    expect((await fetch('/api/things', { method, body: '{}' })).status).toBe(201)
    expect(boundary.requests).toEqual(['/api/things'])
    for (const other of (['POST', 'PUT', 'PATCH', 'DELETE'] as const).filter(candidate => candidate !== method)) {
      await expect(fetch('/api/things', { method: other })).rejects.toThrow('Story network violation')
    }
    expect(boundary.violations).toHaveLength(3)
  })

  it('refuses a GET against a POST-only route', async () => {
    const boundary = installFixtureBoundary({ routes: [writeRoute('POST')] })
    cleanups.push(boundary.dispose)
    await expect(fetch('/api/things')).rejects.toThrow('Story network violation')
    expect(boundary.violations).toHaveLength(1)
    expect(boundary.requests).toEqual([])
  })

  it('holds a never-resolving write until teardown settles it', async () => {
    const boundary = installFixtureBoundary({
      routes: [{ method: 'POST', path: '/api/things', respond: () => new Promise<never>(() => {}) }],
    })
    const pending = fetch('/api/things', { method: 'POST', body: '{}' })
    const settled = vi.fn()
    pending.then(settled, settled)
    await settleQueue()
    expect(settled).not.toHaveBeenCalled()
    const rejection = expect(pending).rejects.toThrow('Fixture disposed')
    boundary.dispose()
    await rejection
  })
})

describe('fake socket semantics', () => {
  function open(script: (socket: FixtureSocket) => void = () => {}) {
    const boundary = installFixtureBoundary({ routes: [], sockets: [{ path: '/ws/a', script }] })
    cleanups.push(boundary.dispose)
    return new WebSocket(`${wsOrigin()}/ws/a`)
  }

  it('drops a server message after close silently, like a real server', async () => {
    const driven: FixtureSocket[] = []
    const fake = open(socket => { driven.push(socket); socket.serverOpen() })
    const received = vi.fn()
    fake.onmessage = received
    fake.close()
    expect(() => driven[0].serverMessage('late')).not.toThrow()
    await settleQueue()
    expect(received).not.toHaveBeenCalled()
  })

  it('throws InvalidStateError for send while CONNECTING', () => {
    const fake = open()
    expect(fake.readyState).toBe(WebSocket.CONNECTING)
    expect(() => fake.send('early')).toThrowError(expect.objectContaining({ name: 'InvalidStateError' }))
  })

  it('exposes protocol, extensions, binaryType and dispatchEvent', async () => {
    const fake = open()
    expect(fake.protocol).toBe('')
    expect(fake.extensions).toBe('')
    expect(fake.binaryType).toBe('blob')
    fake.binaryType = 'arraybuffer'
    expect(fake.binaryType).toBe('arraybuffer')
    fake.binaryType = 'nonsense' as BinaryType
    expect(fake.binaryType).toBe('arraybuffer')
    const heard: string[] = []
    const listener = (event: Event) => heard.push(event.type)
    fake.addEventListener('ping', listener)
    fake.onerror = event => heard.push(`on${event.type}`)
    expect(fake.dispatchEvent(new Event('ping'))).toBe(true)
    fake.dispatchEvent(new Event('error'))
    fake.removeEventListener('ping', listener)
    fake.dispatchEvent(new Event('ping'))
    expect(heard).toEqual(['ping', 'onerror'])
    expect(fake.readyState).toBe(WebSocket.CONNECTING)
    expect(fake).toBeInstanceOf(WebSocket)
  })
})

describe('swallowed violations still fail the story', () => {
  it('records a violation the component catches, and the teardown assertion throws', async () => {
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    await fetch('/api/secret').catch(() => {})
    try { new XMLHttpRequest() } catch { /* swallowed */ }
    try { navigator.sendBeacon('/api/beacon') } catch { /* swallowed */ }
    expect(boundary.violations).toHaveLength(3)
    expect(() => assertNoFixtureViolations(boundary)).toThrow('3 fixture boundary violation')
    expect(() => assertNoFixtureViolations()).toThrow('/api/secret')
  })

  it('passes a clean boundary and no boundary', () => {
    expect(() => assertNoFixtureViolations()).not.toThrow()
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    expect(() => assertNoFixtureViolations()).not.toThrow()
  })

  it('the preview afterEach is wired to the assertion', async () => {
    const boundary = installFixtureBoundary({ routes: [] })
    cleanups.push(boundary.dispose)
    const afterEach = preview.afterEach as unknown as () => void
    expect(afterEach).not.toThrow()
    await fetch('/api/swallowed').catch(() => {})
    expect(afterEach).toThrow('fixture boundary violation')
  })
})

describe('preview decorator fences every story', () => {
  type Decorator = (story: unknown, context: unknown) => Parameters<typeof createApp>[0]
  function render(parameters: Record<string, unknown>, viewMode: 'story' | 'docs') {
    const decorator = (preview.decorators as unknown as Decorator[])[0]
    const host = document.createElement('div')
    const app = createApp(decorator(undefined, { parameters, viewMode }))
    app.mount(host)
    cleanups.push(() => app.unmount())
    return app
  }

  it('fences a story with no parameters.fixture', async () => {
    render({}, 'story')
    const boundary = activeFixtureBoundary()!
    expect(boundary).toBeDefined()
    await expect(fetch('/api/stats?workspace=storybook-alpha&range=30')).rejects.toThrow('Story network violation')
    expect(() => new WebSocket(`${wsOrigin()}/ws/events`)).toThrow('Story network violation')
    expect(boundary.violations).toHaveLength(2)
  })

  it('a bare `fixture: true` flag is not an opt-out and not a config', () => {
    expect(() => render({ fixture: true }, 'story')).toThrow()
  })

  it('gives a story with config only its declared routes', async () => {
    render({ fixture: { routes: statsRoutes('default') } }, 'story')
    expect((await fetch(path)).status).toBe(200)
    await expect(fetch('/api/stats?workspace=personal&range=30')).rejects.toThrow('Story network violation')
    await expect(fetch('/api/tasks')).rejects.toThrow('Story network violation')
    expect(activeFixtureBoundary()!.violations).toHaveLength(2)
  })

  it('fences docs pages, sharing one empty boundary between stories', async () => {
    const first = render({}, 'docs')
    const second = render({}, 'docs')
    const boundary = activeFixtureBoundary()!
    await expect(fetch('/api/anything')).rejects.toThrow('Story network violation')
    expect(boundary.violations).toHaveLength(1)
    first.unmount()
    expect(activeFixtureBoundary()).toBe(boundary)
    second.unmount()
    expect(activeFixtureBoundary()).toBeUndefined()
  })

  it('a docs story that declares routes does not share a boundary', () => {
    render({}, 'docs')
    expect(() => fenceStory({ routes: statsRoutes('default') }, true)).toThrow('already mounted')
  })

  it('mountConnected refuses to mount without a boundary', () => {
    expect(() => mountConnected(document.createElement('div'), { component: HomeTodayStats })).toThrow('needs the fixture boundary')
  })
})
