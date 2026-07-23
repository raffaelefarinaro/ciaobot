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
    const confirmDiscard = vi.fn(() => true)
    vi.stubGlobal('confirm', confirmDiscard)

    const store = useFileViewerStore()
    await store.open('notes/today.md')
    store.startEditing()
    store.editBuffer = 'unsaved draft'

    const opened = await store.open('notes/today.md')

    expect(opened).toBe(false)
    expect(confirmDiscard).not.toHaveBeenCalled()
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
    const confirmDiscard = vi.fn(() => false)
    vi.stubGlobal('confirm', confirmDiscard)

    const store = useFileViewerStore()
    await store.open('notes/first.md')
    store.startEditing()
    store.editBuffer = 'unsaved draft'

    expect(await store.open('notes/second.md')).toBe(false)
    expect(store.path).toBe('notes/first.md')
    expect(store.editBuffer).toBe('unsaved draft')

    confirmDiscard.mockReturnValue(true)
    expect(await store.open('notes/second.md')).toBe(true)
    expect(store.path).toBe('notes/second.md')
    expect(store.content).toBe('second content')
    expect(store.editing).toBe(false)
  })
})
