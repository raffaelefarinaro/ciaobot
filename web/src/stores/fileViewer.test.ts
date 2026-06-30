// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { fileViewerKindForPath, useFileViewerStore } from './fileViewer'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
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
})
