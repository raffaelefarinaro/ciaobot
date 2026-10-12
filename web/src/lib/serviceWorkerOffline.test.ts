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
    addEventListener(name: string, handler: (event: FetchEvent) => void) {
      listeners.set(name, handler)
    },
  }
  const caches = {
    match: vi.fn(async (request: string | Request) => {
      const key = typeof request === 'string' ? request : new URL(request.url).pathname
      return key === '/index.html' || key === '/' ? shell?.clone() : undefined
    }),
  }
  const source = readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8')
  runInNewContext(source, { self, caches, fetch: fetchImpl, URL, Response, Promise })

  const handler = listeners.get('fetch') as FetchHandler | undefined
  if (!handler) throw new Error('service worker did not register a fetch handler')
  return { handler, caches }
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
