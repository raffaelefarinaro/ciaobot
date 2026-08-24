// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { fileViewerKindForPath, useFileViewerStore } from './fileViewer'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('file viewer kind detection', () => {
  test('classifies files by extension', () => {
    expect(fileViewerKindForPath('/tmp/readme.md')).toBe('text')
    expect(fileViewerKindForPath('docs/report.pdf')).toBe('pdf')
    expect(fileViewerKindForPath('docs/presentation.pptx')).toBe('pdf')
    expect(fileViewerKindForPath('Workspace/dashboard.html')).toBe('html')
    expect(fileViewerKindForPath('Workspace/legacy.HTM')).toBe('html')
    expect(fileViewerKindForPath('Workspace/report.html:12')).toBe('html')
  })
})

describe('html artifacts', () => {
  test('opening an artifact does not fetch its source', async () => {
    // The whole point of the lazy path: `error` blanks the viewer body, so a
    // failed or oversized text fetch must not be able to take down a page that
    // renders perfectly well.
    const fetchMock = vi.fn(async () => new Response('', { status: 413 }))
    vi.stubGlobal('fetch', fetchMock)

    const store = useFileViewerStore()
    await store.open('Workspace/dashboard.html', null, 'chat-1')

    expect(store.kind).toBe('html')
    expect(store.htmlView).toBe('preview')
    expect(store.error).toBe('')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test('switching to Code view loads the source once', async () => {
    const fetchMock = vi.fn(async () => new Response('<h1>hi</h1>'))
    vi.stubGlobal('fetch', fetchMock)

    const store = useFileViewerStore()
    await store.open('Workspace/dashboard.html', null, 'chat-1')
    await store.setHtmlView('code')

    expect(store.content).toBe('<h1>hi</h1>')
    expect(store.sourceLoaded).toBe(true)
    expect(fetchMock).toHaveBeenCalledTimes(1)

    await store.setHtmlView('preview')
    await store.setHtmlView('code')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  test('a source failure leaves the rendered page alone', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 413 })))

    const store = useFileViewerStore()
    await store.open('Workspace/huge.html', null, 'chat-1')
    await store.setHtmlView('code')

    expect(store.sourceError).toContain('too large')
    expect(store.error).toBe('')
    expect(store.kind).toBe('html')
  })

  test('editing an artifact requires the source in hand', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<h1>hi</h1>')))

    const store = useFileViewerStore()
    await store.open('Workspace/dashboard.html', null, 'chat-1')

    store.startEditing()
    expect(store.editing).toBe(false)  // Preview view has no source to edit

    await store.setHtmlView('code')
    store.startEditing()
    expect(store.editing).toBe(true)
    expect(store.editBuffer).toBe('<h1>hi</h1>')
  })

  test('saving an artifact bumps the frame token so Preview reloads', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') return new Response('{"ok":true}')
      return new Response('<h1>hi</h1>')
    }))

    const store = useFileViewerStore()
    await store.open('Workspace/dashboard.html')
    await store.setHtmlView('code')
    store.startEditing()
    store.editBuffer = '<h1>edited</h1>'

    const before = store.loadToken
    expect(await store.saveEdits()).toBe(true)
    expect(store.loadToken).toBeGreaterThan(before)
  })
})

describe('file viewer edit mode', () => {
  test('keeps dirty edits when the same file refreshes in the background', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/vault-markdown-paths') {
        return new Response(JSON.stringify({ paths: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response('saved content')
    }))

    const store = useFileViewerStore()
    await store.open('notes/today.md')
    store.startEditing()
    store.editBuffer = 'unsaved draft'

    const opened = await store.open('notes/today.md')

    expect(opened).toBe(false)
    expect(store.path).toBe('notes/today.md')
    expect(store.editing).toBe(true)
    expect(store.editBuffer).toBe('unsaved draft')
  })

  test('asks before replacing a dirty file', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/vault-markdown-paths') {
        return new Response(JSON.stringify({ paths: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response(url.includes('second.md') ? 'second content' : 'first content')
    }))
    const askConfirmModule = await import('../lib/confirm')
    const confirmDiscard = vi.fn(async () => false)
    const spy = vi.spyOn(askConfirmModule, 'askConfirm').mockImplementation(confirmDiscard as never)

    try {
      const store = useFileViewerStore()
      await store.open('notes/first.md')
      store.startEditing()
      store.editBuffer = 'unsaved draft'

      expect(await store.open('notes/second.md')).toBe(false)
      expect(store.path).toBe('notes/first.md')
      expect(store.editBuffer).toBe('unsaved draft')

      confirmDiscard.mockResolvedValue(true)
      expect(await store.open('notes/second.md')).toBe(true)
      expect(store.path).toBe('notes/second.md')
      expect(store.content).toBe('second content')
      expect(store.editing).toBe(false)
    } finally {
      spy.mockRestore()
    }
  })
})

describe('stale responses', () => {
  /** A response the test releases by hand, to hold one fetch open. */
  function gate() {
    let release!: (value: Response) => void
    const promise = new Promise<Response>(r => { release = r })
    return { promise, release }
  }

  test('reopening the same file discards the earlier pending response', async () => {
    // The path-only check could not see this: open A, open B, open A again, and
    // A's FIRST response still matches `path.value`, so it overwrote the newer
    // one — and saveEdits would then post those stale bytes back over the file.
    const firstA = gate()
    let aCalls = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/vault-markdown-paths') {
        return new Response(JSON.stringify({ paths: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.includes('a.md')) {
        aCalls += 1
        return aCalls === 1 ? firstA.promise : new Response('A SECOND')
      }
      return new Response('B CONTENT')
    }))

    const store = useFileViewerStore()
    const staleOpen = store.open('notes/a.md')
    expect(await store.open('notes/b.md')).toBe(true)
    expect(await store.open('notes/a.md')).toBe(true)

    firstA.release(new Response('A STALE'))
    await staleOpen

    expect(store.path).toBe('notes/a.md')
    expect(store.content).toBe('A SECOND')
  })

  test('a slow file response cannot repaint the file opened after it', async () => {
    const slowFile = gate()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === '/api/vault-markdown-paths') {
        return new Response(JSON.stringify({ paths: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.includes('slow.md')) return slowFile.promise
      return new Response('FAST CONTENT')
    }))

    const store = useFileViewerStore()
    const slowOpen = store.open('notes/slow.md')
    expect(await store.open('notes/fast.md')).toBe(true)

    slowFile.release(new Response('SLOW CONTENT'))
    await slowOpen

    expect(store.path).toBe('notes/fast.md')
    expect(store.content).toBe('FAST CONTENT')
  })

  /** Opens an artifact, starts its (held) source fetch, then opens a note. */
  async function abandonedSourceLoad(posted: unknown[]) {
    const slowSource = gate()
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST') {
        posted.push(JSON.parse(String(init.body)))
        return new Response('{"ok":true}')
      }
      if (url.startsWith('/api/file-history')) {
        return new Response(JSON.stringify({ snapshots: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url === '/api/vault-markdown-paths') {
        return new Response(JSON.stringify({ paths: [] }), {
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.includes('dashboard.html')) return slowSource.promise
      return new Response('NOTE CONTENT')
    }))

    const store = useFileViewerStore()
    await store.open('Workspace/dashboard.html', null, 'chat-1')
    const sourceLoad = store.setHtmlView('code')

    // The user moves on before the artifact's source arrives.
    expect(await store.open('notes/b.md', null, 'chat-1')).toBe(true)

    slowSource.release(new Response('<h1>ARTIFACT SOURCE</h1>'))
    await sourceLoad
    return store
  }

  test('a slow artifact source cannot repaint the note opened after it', async () => {
    const store = await abandonedSourceLoad([])

    expect(store.content).toBe('NOTE CONTENT')
    expect(store.sourceLoaded).toBe(false)
  })

  test('a stale source can never be saved over the file that is open', async () => {
    // The damaging half of the race: `content` is exactly what saveEdits POSTs
    // to `path`, so a stale response landing in `content` writes the
    // artifact's bytes over the note the user actually has open.
    const posted: unknown[] = []
    const store = await abandonedSourceLoad(posted)

    store.startEditing()
    expect(await store.saveEdits()).toBe(true)
    expect(posted).toEqual([
      { chat_id: 'chat-1', path: 'notes/b.md', content: 'NOTE CONTENT' },
    ])
  })
})
