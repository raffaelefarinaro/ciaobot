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
    expect(fileViewerKindForPath('memory-vault/Ideas/map.excalidraw')).toBe('excalidraw')
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
  test('starts editing mode for excalidraw diagrams', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{"type":"excalidraw","elements":[]}')))

    const store = useFileViewerStore()
    await store.open('diagram.excalidraw', null, 'chat-1')
    store.startEditing()

    expect(store.kind).toBe('excalidraw')
    expect(store.editing).toBe(true)
  })

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
