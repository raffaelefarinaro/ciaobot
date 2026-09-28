import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'

describe('service worker share target', () => {
  it('intercepts a multipart POST, stages it locally, and redirects to Home', async () => {
    const listeners = new Map<string, (event: { request: Request; respondWith: (response: Promise<Response>) => void }) => void>()
    const records = new Map<string, unknown>()
    const store = {
      getAll() {
        const result = { result: [...records.values()], onsuccess: null as null | (() => void) }
        queueMicrotask(() => result.onsuccess?.())
        return result
      },
      put(value: { id: string }) { records.set(value.id, value) },
      delete(id: string) { records.delete(id) },
    }
    const db = {
      createObjectStore() {},
      transaction() {
        const transaction = {
          objectStore: () => store,
          oncomplete: null as null | (() => void),
          onerror: null,
        }
        setTimeout(() => transaction.oncomplete?.(), 0)
        return transaction
      },
      close() {},
    }
    const indexedDB = {
      open() {
        const request = {
          result: db,
          onupgradeneeded: null as null | (() => void),
          onsuccess: null as null | (() => void),
          onerror: null,
        }
        queueMicrotask(() => { request.onupgradeneeded?.(); request.onsuccess?.() })
        return request
      },
    }
    const self = {
      location: { origin: 'https://ciao.example' },
      crypto: { randomUUID: () => 'share-id' },
      addEventListener(name: string, handler: typeof listeners extends Map<string, infer V> ? V : never) { listeners.set(name, handler) },
    }
    runInNewContext(readFileSync(new URL('../../public/sw.js', import.meta.url), 'utf8'), {
      self, indexedDB, File, URL, Response, Date, caches: {}, queueMicrotask,
    })
    const form = new FormData()
    form.set('title', 'An article')
    form.set('files', new File(['content'], 'note.txt', { type: 'text/plain' }))
    const request = new Request('https://ciao.example/share-target', { method: 'POST', body: form })
    const responses: Promise<Response>[] = []
    listeners.get('fetch')!({ request, respondWith: response => { responses.push(response) } })
    const response = await responses.at(-1)
    expect(response?.status).toBe(303)
    expect(response?.headers.get('location')).toBe('https://ciao.example/?shared=share-id')
    expect(records.get('share-id')).toMatchObject({ title: 'An article', files: [expect.objectContaining({ name: 'note.txt' })] })

    responses.length = 0
    const oversized = new FormData()
    oversized.set('files', new File([new Uint8Array(20 * 1024 * 1024 + 1)], 'large.pdf', { type: 'application/pdf' }))
    listeners.get('fetch')!({
      request: new Request('https://ciao.example/share-target', { method: 'POST', body: oversized }),
      respondWith: response => { responses.push(response) },
    })
    expect((await responses.at(-1))?.headers.get('location')).toBe('https://ciao.example/?share-error=size')
    expect(records.size).toBe(1)

    responses.length = 0
    listeners.get('fetch')!({
      request: new Request('https://other.example/share-target', { method: 'POST', body: form }),
      respondWith: response => { responses.push(response) },
    })
    expect(responses).toHaveLength(0)
  })
})
