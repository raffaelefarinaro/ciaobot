// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { installStatsBoundary } from './statsBoundary'
import { mountConnectedStats } from './connectedStats'

const cleanups: Array<() => void> = []
afterEach(() => { cleanups.splice(0).reverse().forEach(cleanup => cleanup()); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const path = '/api/stats?workspace=storybook-alpha&range=30'

describe('connected stats transport boundary', () => {
  it('answers only exact declared synthetic GETs without native fetch', async () => {
    const native = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('native transport'))
    const boundary = installStatsBoundary('default')
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
    const boundary = installStatsBoundary('default')
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
    const boundary = installStatsBoundary('loading')
    expect(() => installStatsBoundary('default')).toThrow('already mounted')
    const pending = fetch(path)
    const rejection = expect(pending).rejects.toThrow('disposed')
    boundary.dispose()
    boundary.dispose()
    await rejection
    expect(Object.getOwnPropertyDescriptor(globalThis, 'fetch')).toEqual(fetchBefore)
    const fresh = installStatsBoundary('default')
    cleanups.push(fresh.dispose)
    expect(fresh.violations).toEqual([])
  })

  it('records even a captured fixture fetch after teardown rather than falling through to native', async () => {
    const native = vi.spyOn(globalThis, 'fetch')
    const boundary = installStatsBoundary('default')
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
    const boundary = installStatsBoundary('default')
    cleanups.push(boundary.dispose)
    const origin = location.origin.replace(/^http/, 'ws')
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
    const boundary = installStatsBoundary('default')
    cleanups.push(boundary.dispose)
    const origin = location.origin.replace(/^http/, 'ws')
    const runner = { url: `${origin}/__vitest_browser_api__?type=tester&rpcId=rpc-1&sessionId=session-1&projectName=&method=tasks&token=0` }
    const send = socket.prototype.send
    const request = '[{"m":"1","a":"2","t":"3","i":"4"},"onCollected",["5"],"q","synthetic-id","synthetic"]'
    const response = '[{"t":"1","i":"2","r":"3"},"s","synthetic-id",null]'
    send.call(runner, request)
    send.call(runner, response)
    expect(nativeSend).toHaveBeenCalledTimes(2)
    expect(boundary.violations).toEqual([])
    for (const [target, data] of [
      [{ ...runner, url: runner.url.replace(origin, 'wss://example.com') }, request],
      [{ ...runner, url: runner.url.replace('/__vitest_browser_api__', '/api/write') }, request],
      [{ ...runner, url: runner.url.replace('&token=0', '&page=preview') }, request],
      [{ ...runner, url: runner.url.replace('type=tester&', '') }, request],
      [runner, '{"type":"saveStory"}'],
      [runner, '[{"m":"1","t":"2","i":"3"},0,"q","id"]'],
      [runner, '[{"m":"1","t":"2","i":"3"},"onCollected","s","id"]'],
      [runner, '[{"m":"1","a":"2","t":"3","i":"4","x":"5"},"m",[],"q","id","extra"]'],
    ] as const) expect(() => send.call(target, data)).toThrow('Story network violation')
    expect(nativeSend).toHaveBeenCalledTimes(2)
    expect(boundary.violations).toHaveLength(8)
  })

  it('permits only established same-origin workshop lifecycle/heartbeat sends, never custom or write events', () => {
    const nativeSend = vi.fn()
    const socket = vi.fn(function () {})
    socket.prototype.send = nativeSend
    vi.stubGlobal('WebSocket', socket)
    vi.stubGlobal('CHANNEL_OPTIONS', { wsToken: 'synthetic-workshop-token' })
    const boundary = installStatsBoundary('default')
    cleanups.push(boundary.dispose)
    const origin = location.origin.replace(/^http/, 'ws')
    const preview = { url: `${origin}/storybook-server-channel?token=synthetic-workshop-token&page=preview`, protocol: '' }
    const send = socket.prototype.send
    send.call(preview, '{"type":"storyRendered","args":["synthetic-story"]}')
    send.call({ url: `${origin}/?token=synthetic-hmr-token`, protocol: 'vite-hmr' }, '{"type":"ping"}')
    expect(nativeSend).toHaveBeenCalledTimes(2)
    for (const [target, data] of [
      [preview, '{"type":"custom","event":"saveStory"}'],
      [preview, '{"type":"saveStory"}'],
      [{ ...preview, url: preview.url.replace('preview', 'manager') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace('synthetic-workshop-token', 'wrong') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace('/storybook-server-channel', '/api/write') }, '{"type":"storyRendered"}'],
      [{ ...preview, url: preview.url.replace(origin, 'wss://example.com') }, '{"type":"storyRendered"}'],
      [{ url: `${origin}/?token=synthetic-hmr-token`, protocol: 'vite-hmr' }, '{"type":"custom"}'],
    ] as const) expect(() => send.call(target, data)).toThrow('Story network violation')
    expect(nativeSend).toHaveBeenCalledTimes(2)
    expect(boundary.violations).toHaveLength(7)
  })
})

async function mount(state: 'default' | 'loading' | 'failed' | 'partial', declared = true) {
  const host = document.createElement('div')
  document.body.append(host)
  const fixture = mountConnectedStats(host, state, declared ? 'storybook-alpha' : 'undeclared')
  cleanups.push(() => { fixture.dispose(); host.remove() })
  await fixture.router.isReady()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
  return { ...fixture, host }
}

describe('real connected owner', () => {
  it('uses normalization, memory navigation, fresh store and workspace-scoped fixtures', async () => {
    const first = await mount('default')
    expect(first.host.querySelector('strong')?.textContent).toBe('12')
    first.projects.activeWorkspace = 'storybook-beta'
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
    const second = await mount('default')
    expect(second.pinia).not.toBe(first.pinia)
    expect(second.projects.activeWorkspace).toBe('storybook-alpha')
    expect(second.router.currentRoute.value.path).toBe('/')
    expect(second.host.querySelector('strong')?.textContent).toBe('12')
  })

  it.each(['loading', 'failed'] as const)('%s keeps the real owner’s six dashes without invented status UI', async state => {
    const fixture = await mount(state)
    expect([...fixture.host.querySelectorAll('strong')].map(el => el.textContent)).toEqual(Array(6).fill('—'))
    expect(fixture.host.querySelectorAll('.home-today-avg')).toHaveLength(0)
    expect(fixture.boundary.requests).toEqual([path])
    expect(fixture.boundary.violations).toEqual([])
  })

  it('normalizes missing and invalid fields, keeping valid zeroes and averages', async () => {
    const fixture = await mount('partial')
    expect([...fixture.host.querySelectorAll('strong')].map(el => el.textContent)).toEqual(['0', '—', '—', '3', '—', '—'])
    expect(fixture.host.querySelector('.home-today-avg')?.textContent).toBe('avg 0.4')
  })

  it('exposes violations even when HomeTodayStats catches the rejection', async () => {
    const fixture = await mount('default', false)
    expect(fixture.host.querySelector('strong')?.textContent).toBe('—')
    expect(fixture.boundary.violations).toHaveLength(1)
    expect(fixture.boundary.violations[0]).toContain('undeclared')
  })
})
