import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { describe, expect, it, vi } from 'vitest'

type ServiceWorkerRequest = { url: string; method: string; mode: string }
type FetchEvent = {
  request: ServiceWorkerRequest
  respondWith: (response: Promise<Response> | Response) => void
}
type FetchHandler = (event: FetchEvent) => void

function loadFetchHandler(fetchImpl: typeof fetch, shell?: Response) {
  const listeners = new Map<string, (event: FetchEvent) => void>()
  const self = {
    location: { origin: 'https://ciao.example' },
    skipWaiting: () => undefined,
    addEventListener(name: string, handler: (event: FetchEvent) => void) {
      listeners.set(name, handler)
    },
  }
  const addAll = vi.fn(async (_assets: string[]) => undefined)
  const caches = {
    match: vi.fn(async (request: string | Request) => {
      const key = typeof request === 'string' ? request : new URL(request.url).pathname
      return key === '/index.html' || key === '/' ? shell?.clone() : undefined
    }),
    open: vi.fn(async (_name: string) => ({ addAll, put: vi.fn(async () => undefined) })),
  }
  const source = readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8')
  runInNewContext(source, { self, caches, fetch: fetchImpl, URL, Response, Promise })

  const handler = listeners.get('fetch') as FetchHandler | undefined
  if (!handler) throw new Error('service worker did not register a fetch handler')
  const install = listeners.get('install') as ((event: { waitUntil: (p: Promise<unknown>) => void }) => void) | undefined
  return { handler, caches, addAll, install }
}

function dispatch(handler: FetchHandler, request: ServiceWorkerRequest): Promise<Response> | null {
  let response: Promise<Response> | Response | undefined
  handler({ request, respondWith: value => { response = value } })
  return response ? Promise.resolve(response) : null
}

describe('service worker offline navigation', () => {
  it.each([502, 503, 504])('serves the cached app shell for navigation status %i', async status => {
    const shell = new Response('cached app shell', { status: 200 })
    const fetchMock = vi.fn(async () => new Response('proxy error', { status }))
    const { handler, caches } = loadFetchHandler(fetchMock as unknown as typeof fetch, shell)
    const responsePromise = dispatch(handler, {
      url: 'https://ciao.example/chat/chat-1', method: 'GET', mode: 'navigate',
    })

    expect(responsePromise).not.toBeNull()
    const response = await responsePromise!
    expect(response.status).toBe(200)
    expect(await response.text()).toBe('cached app shell')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(caches.match).toHaveBeenCalledWith('/index.html')
  })

  it('keeps a healthy navigation response and skips the shell cache', async () => {
    const fetchMock = vi.fn(async () => new Response('live page', { status: 200 }))
    const { handler, caches } = loadFetchHandler(
      fetchMock as unknown as typeof fetch,
      new Response('cached app shell'),
    )
    const responsePromise = dispatch(handler, {
      url: 'https://ciao.example/chat/chat-1', method: 'GET', mode: 'navigate',
    })

    expect(responsePromise).not.toBeNull()
    const response = await responsePromise!
    expect(response.status).toBe(200)
    expect(await response.text()).toBe('live page')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(caches.match).not.toHaveBeenCalled()
  })

  it('keeps a proxy error when the cached app shell is unavailable', async () => {
    const fetchMock = vi.fn(async () => new Response('proxy error', { status: 503 }))
    const { handler, caches } = loadFetchHandler(fetchMock as unknown as typeof fetch)
    const responsePromise = dispatch(handler, {
      url: 'https://ciao.example/chat/chat-1', method: 'GET', mode: 'navigate',
    })

    expect(responsePromise).not.toBeNull()
    const response = await responsePromise!
    expect(response.status).toBe(503)
    expect(await response.text()).toBe('proxy error')
    expect(caches.match).toHaveBeenNthCalledWith(1, '/index.html')
    expect(caches.match).toHaveBeenNthCalledWith(2, '/')
  })

  it('keeps a non-navigation proxy error even when the shell is cached', async () => {
    const fetchMock = vi.fn(async () => new Response('proxy error', { status: 503 }))
    const { handler, caches } = loadFetchHandler(
      fetchMock as unknown as typeof fetch,
      new Response('cached app shell'),
    )
    const responsePromise = dispatch(handler, {
      url: 'https://ciao.example/chat/chat-1', method: 'GET', mode: 'cors',
    })

    expect(responsePromise).not.toBeNull()
    const response = await responsePromise!
    expect(response.status).toBe(503)
    expect(await response.text()).toBe('proxy error')
    expect(caches.match).not.toHaveBeenCalled()
  })

  it.each(['/api/startup-status', '/ws/events'])('leaves %s outside service worker interception', async path => {
    const fetchMock = vi.fn(async () => new Response('network response', { status: 200 }))
    const { handler, caches } = loadFetchHandler(fetchMock as unknown as typeof fetch, new Response('shell'))
    const response = dispatch(handler, {
      url: `https://ciao.example${path}`, method: 'GET', mode: 'cors',
    })

    expect(response).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(caches.match).not.toHaveBeenCalled()
  })
})

describe('service worker brand artwork', () => {
  const brandPaths = ['/brand/ciaobot-wordmark.png', '/brand/ciaobot-wordmark-light.png']

  it('precaches both wordmark images at install so the shell still shows the logo offline', async () => {
    const { addAll, install } = loadFetchHandler(vi.fn() as unknown as typeof fetch)
    let pending: Promise<unknown> = Promise.resolve()
    install!({ waitUntil: p => { pending = p } })
    await pending

    expect(addAll).toHaveBeenCalledTimes(1)
    const assets = addAll.mock.calls[0][0]
    for (const path of brandPaths) expect(assets).toContain(path)
  })

  it.each(brandPaths)('serves %s cache-first without refetching it', async path => {
    const fetchMock = vi.fn(async () => new Response('png-bytes', { status: 200 }))
    const { handler, caches } = loadFetchHandler(fetchMock as unknown as typeof fetch)
    const first = dispatch(handler, { url: `https://ciao.example${path}`, method: 'GET', mode: 'no-cors' })
    expect(first).not.toBeNull()
    expect((await first!).status).toBe(200)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(caches.match).toHaveBeenCalledTimes(1)
  })
})
