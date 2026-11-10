// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { flushPromises } from '@vue/test-utils'
import { fileViewerKindForPath, isPinnableFilePath, useFileViewerStore } from './fileViewer'
import { useProjectStore } from './projects'
import { api } from '../lib/api'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  const originalGet = api.get
  vi.spyOn(api, 'get').mockImplementation((url) => {
    if (/\/api\/chats\/[^/]+\/file-path\?/.test(url)) {
      return Promise.resolve({ path: new URL(url, 'http://localhost').searchParams.get('path') })
    }
    return originalGet(url)
  })
})

describe('file viewer kind detection', () => {
  test('resolves a chat link before loading the canonical file', async () => {
    vi.mocked(api.get).mockResolvedValue({ path: '/install/work/memory-vault/draft.md' })
    const fetchMock = vi.fn(async (_url: string) => new Response('Draft'))
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.open('memory-vault/draft.md', null, 'chat-work')
    expect(api.get).toHaveBeenCalledWith('/api/chats/chat-work/file-path?path=memory-vault%2Fdraft.md')
    expect(store.path).toBe('/install/work/memory-vault/draft.md')
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/workspace-file?path=%2Finstall%2Fwork%2Fmemory-vault%2Fdraft.md')
    expect(store.content).toBe('Draft')
  })
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

  test('keeps a dirty file open when close is declined and closes after confirmation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('saved content')))
    const askConfirmModule = await import('../lib/confirm')
    const confirmDiscard = vi.fn()
      .mockResolvedValueOnce(false)
      .mockResolvedValueOnce(true)
    const spy = vi.spyOn(askConfirmModule, 'askConfirm').mockImplementation(confirmDiscard as never)

    try {
      const store = useFileViewerStore()
      await store.open('notes/today.txt')
      store.startEditing()
      store.editBuffer = 'unsaved draft'

      expect(await store.close()).toBe(false)
      expect(store.isOpen).toBe(true)
      expect(store.path).toBe('notes/today.txt')
      expect(store.editing).toBe(true)
      expect(store.editBuffer).toBe('unsaved draft')

      expect(await store.close()).toBe(true)
      expect(store.isOpen).toBe(false)
      expect(store.path).toBe('')
      expect(store.editing).toBe(false)
      expect(confirmDiscard).toHaveBeenCalledTimes(2)
    } finally {
      spy.mockRestore()
    }
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

describe('shared-pin surface', () => {
  function seedChat(chatId: string) {
    const store = useProjectStore()
    store.chats = [{
      chat_id: chatId,
      project_id: 'p1',
      title: 'T',
      model: 'm',
      provider: 'claude',
      mode: '',
      session_id: '',
      created_at: '',
      archived: false,
    }]
    return store
  }

  test('remote close reconciles the shared-pin preview but not an ordinary one', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('content')))
    const projectStore = seedChat('chat-1')
    projectStore.applyChatPinState('chat-1', { path: 'notes/a.txt', dismissed_paths: [], revision: 1 }, { source: 'event' })
    const store = useFileViewerStore()

    await store.openSharedPin('notes/a.txt', 'chat-1')
    expect(store.sharedPinChatId).toBe('chat-1')
    expect(store.isOpen).toBe(true)

    projectStore.applyChatPinState('chat-1', { path: '', dismissed_paths: ['notes/a.txt'], revision: 2 }, { source: 'event' })
    await nextTick()
    await nextTick()
    expect(store.isOpen).toBe(false)

    // An ordinary manual open is not the shared pin; a remote change leaves it.
    await store.open('notes/b.txt', null, 'chat-1')
    expect(store.isOpen).toBe(true)
    projectStore.applyChatPinState('chat-1', { path: '/x.md', dismissed_paths: [], revision: 3 }, { source: 'event' })
    await nextTick()
    await nextTick()
    expect(store.isOpen).toBe(true)
  })

  test('remote close while dirty preserves the buffer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('saved content')))
    const projectStore = seedChat('chat-1')
    projectStore.applyChatPinState('chat-1', { path: 'notes/a.txt', dismissed_paths: [], revision: 1 }, { source: 'event' })
    const store = useFileViewerStore()

    await store.openSharedPin('notes/a.txt', 'chat-1')
    store.startEditing()
    store.editBuffer = 'unsaved draft'
    expect(store.isDirty).toBe(true)

    projectStore.applyChatPinState('chat-1', { path: '', dismissed_paths: ['notes/a.txt'], revision: 2 }, { source: 'event' })
    await nextTick()
    await nextTick()

    // The editor and its buffer survive; the open is demoted to ordinary.
    expect(store.isOpen).toBe(true)
    expect(store.editBuffer).toBe('unsaved draft')
    expect(store.sharedPinChatId).toBe('')
  })

  test('local dismissal of the shared preview issues one unpin', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('content')))
    const projectStore = seedChat('chat-1')
    projectStore.applyChatPinState('chat-1', { path: 'notes/a.txt', dismissed_paths: [], revision: 1 }, { source: 'event' })
    const store = useFileViewerStore()

    await store.openSharedPin('notes/a.txt', 'chat-1')
    const unpin = vi.spyOn(projectStore, 'unpinFile').mockResolvedValue(undefined)

    await store.dismissSharedPin()

    expect(unpin).toHaveBeenCalledTimes(1)
    expect(unpin).toHaveBeenCalledWith('chat-1')
    expect(store.isOpen).toBe(false)
  })

  test('event-driven close issues no unpin', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('content')))
    const projectStore = seedChat('chat-1')
    projectStore.applyChatPinState('chat-1', { path: 'notes/a.txt', dismissed_paths: [], revision: 1 }, { source: 'event' })
    const store = useFileViewerStore()

    await store.openSharedPin('notes/a.txt', 'chat-1')
    const unpin = vi.spyOn(projectStore, 'unpinFile').mockResolvedValue(undefined)

    projectStore.applyChatPinState('chat-1', { path: '', dismissed_paths: ['notes/a.txt'], revision: 2 }, { source: 'event' })
    await nextTick()
    await nextTick()

    expect(store.isOpen).toBe(false)
    expect(unpin).not.toHaveBeenCalled()
  })
})

describe('docking beside the chat', () => {
  test('a file a chat opens is pinned without opening the viewer', async () => {
    vi.mocked(api.get).mockResolvedValue({ path: '/work/notes/today.md' })
    const fetchMock = vi.fn(async () => new Response('Hello'))
    vi.stubGlobal('fetch', fetchMock)
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    const opened = store.open('notes/today.md', null, 'chat-1')
    // Never shown, not even while the pin write is in flight.
    expect(store.isOpen).toBe(false)
    expect(await opened).toBe(true)
    expect(store.isOpen).toBe(false)
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/work/notes/today.md')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test('a file opened from the chat beside opens the viewer instead of pinning onto the main chat', async () => {
    vi.mocked(api.get).mockResolvedValue({ path: '/work/notes/today.md' })
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.open('notes/today.md', null, 'chat-2')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
  })

  test('an open that targets a line keeps the viewer, which scrolls to it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.open('notes/today.md', 120, 'chat-1')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
    expect(store.line).toBe(120)
  })

  test('a chat open replaces the current pin', async () => {
    vi.mocked(api.get).mockResolvedValue({ path: '/work/b.md' })
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinnedFileFor').mockReturnValue('/work/a.md')
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.openImage('b.md', 'chat-1')
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/work/b.md')
    expect(store.isOpen).toBe(false)
  })

  test('a link followed from the pinned file opens over it', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Linked')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinnedFileFor').mockReturnValue('/work/a.md')
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.open('/work/linked.md')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
    expect(store.content).toBe('Linked')
  })

  test('a failed pin falls back to the viewer', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(false)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.open('notes/today.md', null, 'chat-1')
    expect(projects.pinFile).toHaveBeenCalledTimes(1)
    expect(store.isOpen).toBe(true)
    expect(store.content).toBe('Hello')
  })

  test('a slow dock never pins over a newer open', async () => {
    let finishFirst!: (v: { path: string }) => void
    vi.mocked(api.get)
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve as typeof finishFirst }))
      .mockResolvedValueOnce({ path: '/work/b.md' })
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    const first = store.open('a.md', null, 'chat-1')
    await store.open('b.md', null, 'chat-1')
    finishFirst({ path: '/work/a.md' })
    expect(await first).toBe(true)
    expect(projects.pinFile).toHaveBeenCalledTimes(1)
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/work/b.md')
    expect(store.isOpen).toBe(false)
  })

  test('an open without a chat never docks, even when nothing is pinned', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Archived')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinnedFileFor').mockReturnValue(undefined)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.open('memory-vault/Logs/Chats/chat-abc/claude/2026-10-01-s.md')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
    expect(store.content).toBe('Archived')
  })

  test('an image opened without a chat uses the viewer', async () => {
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinnedFileFor').mockReturnValue(undefined)
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.openImage('a.png')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
    expect(store.kind).toBe('image')
  })

  test('without a dock the viewer opens and nothing is pinned', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    await store.open('notes/today.md', null, 'chat-1')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
  })

  test('a folder a chat opens docks into the tile', async () => {
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    expect(await store.openFolder('/ws/exports', 'chat-1')).toBe(true)
    expect(store.isOpen).toBe(false)
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/ws/exports/')
  })

  test('Browse files resolves the root before docking', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ path: '/ws' }))))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.openFolder('', 'chat-1')
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/ws/')
    expect(store.isOpen).toBe(false)
  })

  test('reopening the pinned folder resets the tile instead of pinning', async () => {
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinnedFileFor').mockReturnValue('/ws/exports/')
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    expect(await store.openFolder('/ws/exports', 'chat-1')).toBe(true)
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(false)
    expect(store.folderRedock).toEqual({ path: '/ws/exports/', seq: 1 })
  })

  test('a failed folder pin falls back to the viewer', async () => {
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(false)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    expect(await store.openFolder('/ws/exports', 'chat-1')).toBe(true)
    expect(store.isOpen).toBe(true)
    expect(store.kind).toBe('folder')
    expect(store.path).toBe('/ws/exports')
  })

  test('a folder opened from another chat uses the viewer', async () => {
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.openFolder('/ws/exports', 'chat-2')
    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(store.isOpen).toBe(true)
  })

  test('a file docked from a folder records no viewer Back', async () => {
    vi.mocked(api.get).mockResolvedValue({ path: '/ws/exports/a.md' })
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const store = useFileViewerStore()
    store.setDockTarget('chat-1')
    await store.openFromFolder('/ws/exports/a.md', 'chat-1', '/ws/exports')
    expect(projects.pinFile).toHaveBeenCalledWith('chat-1', '/ws/exports/a.md')
    expect(store.isOpen).toBe(false)
    expect(store.returnFolder).toBe('')
  })

  test('isPinnableFilePath mirrors the server allowlist', () => {
    expect(isPinnableFilePath('a.md')).toBe(true)
    expect(isPinnableFilePath('x/y.HTML')).toBe(true)
    expect(isPinnableFilePath('b.csv:3')).toBe(true)
    expect(isPinnableFilePath('p.png')).toBe(false)
    expect(isPinnableFilePath('d.pdf')).toBe(false)
    expect(isPinnableFilePath('w.docx')).toBe(false)
    expect(isPinnableFilePath('Makefile')).toBe(false)
    expect(isPinnableFilePath('dir/')).toBe(false)
  })
})

describe('served images', () => {
  test('openImageUrl shows a served image without a workspace fetch', () => {
    const store = useFileViewerStore()
    store.openImageUrl('/api/images/upload-sample.png', 'upload-sample.png', 'chat-1')
    expect(store.isOpen).toBe(true)
    expect(store.kind).toBe('image')
    expect(store.path).toBe('upload-sample.png')
    expect(store.imageUrl).toBe('/api/images/upload-sample.png')
    expect(store.loading).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })

  test('a later workspace openImage clears imageUrl', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const store = useFileViewerStore()
    store.openImageUrl('/api/images/upload-sample.png', 'upload-sample.png')
    await store.openImage('photo.png')
    expect(store.imageUrl).toBe('')
  })
})

describe('folder browser', () => {
  test('openFolder shows a folder without fetching', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    expect(await store.openFolder('/ws/exports', 'chat-1')).toBe(true)
    expect(store.isOpen).toBe(true)
    expect(store.kind).toBe('folder')
    expect(store.path).toBe('/ws/exports')
    expect(store.chatId).toBe('chat-1')
    expect(store.loading).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  test('openFolder asks before discarding unsaved edits', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('x')))
    const askConfirmModule = await import('../lib/confirm')
    const spy = vi.spyOn(askConfirmModule, 'askConfirm').mockImplementation((async () => false) as never)
    try {
      const store = useFileViewerStore()
      await store.open('/ws/a.txt')
      store.editing = true
      store.editBuffer = 'changed'
      expect(await store.openFolder('/ws')).toBe(false)
      expect(store.kind).toBe('text')
      expect(store.editBuffer).toBe('changed')
    } finally {
      spy.mockRestore()
    }
  })

  test('setFolderPath follows the browser only while a folder is shown', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Hello')))
    const store = useFileViewerStore()
    await store.open('/ws/a.txt')
    store.setFolderPath('/x')
    expect(store.path).toBe('/ws/a.txt')
    await store.openFolder('/ws')
    store.setFolderPath('/ws/sub')
    expect(store.path).toBe('/ws/sub')
  })

  test('Back from a file opened in the browser returns to the folder', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('plain text')))
    const store = useFileViewerStore()
    await store.openFolder('/ws/notes', 'chat-1')
    await store.openFromFolder('/ws/notes/a.txt', 'chat-1')
    expect(store.kind).toBe('text')
    expect(store.returnFolder).toBe('/ws/notes')

    expect(await store.backOrClose()).toBe(false)
    expect(store.kind).toBe('folder')
    expect(store.path).toBe('/ws/notes')

    expect(await store.backOrClose()).toBe(true)
    expect(store.isOpen).toBe(false)
    expect(store.returnFolder).toBe('')
  })

  test('Back while the file is still loading returns to the folder', async () => {
    let finish: (r: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((resolve) => { finish = resolve })))
    const store = useFileViewerStore()
    await store.openFolder('/ws/notes', 'chat-1')
    const loading = store.openFromFolder('/ws/notes/a.txt', 'chat-1')
    await flushPromises()
    expect(store.returnFolder).toBe('/ws/notes')

    expect(await store.backOrClose()).toBe(false)
    expect(store.kind).toBe('folder')
    finish(new Response('late text'))
    await loading
    expect(store.kind).toBe('folder')
    expect(store.content).toBe('')
  })

  test('a link followed inside the file keeps the way back', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('plain text')))
    const store = useFileViewerStore()
    await store.openFolder('/ws/notes', 'chat-1')
    await store.openFromFolder('/ws/notes/a.txt', 'chat-1')
    await store.open('/ws/notes/b.txt', null, 'chat-1')
    expect(store.returnFolder).toBe('/ws/notes')
  })

  test('closing forgets the folder', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('plain text')))
    const store = useFileViewerStore()
    await store.openFolder('/ws/notes', 'chat-1')
    await store.openFromFolder('/ws/notes/a.txt', 'chat-1')
    await store.close()
    expect(store.returnFolder).toBe('')
  })

  test('a picture chosen in the browser opens as an image', async () => {
    const store = useFileViewerStore()
    await store.openFolder('/ws')
    await store.openFromFolder('/ws/p.png')
    expect(store.kind).toBe('image')
  })
})

describe('opening links', () => {
  const dirListing = (path: string) => new Response(JSON.stringify({
    path,
    root: { name: 'workspace', path: '/ws' },
    roots: [{ name: 'workspace', path: '/ws' }],
    parent: '/ws/exports',
    entries: [],
    truncated: false,
  }), { status: 200 })

  test('a folder link opens the folder browser', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.startsWith('/api/workspace-dir')) return dirListing('/ws/exports/sloth-v5')
      return new Response('', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.openLink('exports/sloth-v5', null, 'chat-1')
    expect(store.kind).toBe('folder')
    expect(store.path).toBe('/ws/exports/sloth-v5')
    const probe = new URL(String(fetchMock.mock.calls[0]?.[0]), 'http://localhost')
    expect(probe.pathname).toBe('/api/workspace-dir')
    expect(probe.searchParams.get('path')).toBe('exports/sloth-v5')
    expect(probe.searchParams.get('chat_id')).toBe('chat-1')
  })

  test("an extensionless file keeps today's open and its error", async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.startsWith('/api/workspace-dir')) {
        return new Response(JSON.stringify({ error: 'not_a_directory', kind: 'file', path: '/ws/bin/run' }), { status: 400 })
      }
      return new Response('', { status: 415 })
    }))
    const store = useFileViewerStore()
    await store.openLink('bin/run', null, '')
    expect(store.kind).toBe('text')
    expect(store.path).toBe('bin/run')
    expect(store.error).toBe('Unsupported file type.')
  })

  test('a known extensionless name never probes', async () => {
    const fetchMock = vi.fn(async (_url: string) => new Response('all:'))
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.openLink('docs/Makefile', null, '')
    const urls = fetchMock.mock.calls.map(call => String(call[0]))
    expect(urls.some(url => url.startsWith('/api/workspace-dir'))).toBe(false)
    expect(urls[0]).toBe('/api/workspace-file?path=docs%2FMakefile')
  })

  test("a missing folder falls back to the viewer's not-found message", async () => {
    vi.stubGlobal('fetch', vi.fn(async (_url: string) => new Response('', { status: 404 })))
    const store = useFileViewerStore()
    await store.openLink('exports/gone', null, '')
    expect(store.isOpen).toBe(true)
    expect(store.kind).toBe('text')
    expect(store.error).toBe('File not found.')
  })

  test('a folder probe that resolves after a newer click cannot repaint the viewer', async () => {
    let answerProbe: (resp: Response) => void = () => {}
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (url.startsWith('/api/workspace-dir')) {
        return new Promise<Response>(resolve => { answerProbe = resolve })
      }
      return Promise.resolve(new Response('note'))
    }))
    const store = useFileViewerStore()
    const folder = store.openLink('exports/sloth', null, '')
    await store.openLink('a.md')
    answerProbe(dirListing('/ws/exports/sloth'))
    await folder
    expect(store.kind).toBe('text')
    expect(store.path).toBe('a.md')
  })

  test('a folder outside the browsable roots says so instead of Unsupported file type', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.startsWith('/api/workspace-dir')) {
        return new Response(JSON.stringify({ error: 'outside_roots' }), { status: 403 })
      }
      return new Response('', { status: 415 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.openLink('/elsewhere/dir', null, '')
    expect(store.isOpen).toBe(true)
    expect(store.error).toBe('This folder is outside the folders Ciaobot can browse.')
    const urls = fetchMock.mock.calls.map(call => String(call[0]))
    expect(urls.some(url => url.startsWith('/api/workspace-file'))).toBe(false)
  })

  test('a link with an extension skips the probe', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.startsWith('/api/vault-markdown-paths')) return new Response(JSON.stringify({ paths: [] }))
      return new Response('note')
    })
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.openLink('notes/a.md', null, '')
    const urls = fetchMock.mock.calls.map(call => String(call[0]))
    expect(urls.some(url => url.startsWith('/api/workspace-dir'))).toBe(false)
    expect(store.content).toBe('note')
  })

  test('an image link opens as an image', async () => {
    const fetchMock = vi.fn(async (_url: string) => new Response(''))
    vi.stubGlobal('fetch', fetchMock)
    const store = useFileViewerStore()
    await store.openLink('shots/a.png')
    expect(store.kind).toBe('image')
    const urls = fetchMock.mock.calls.map(call => String(call[0]))
    expect(urls.some(url => url.startsWith('/api/workspace-dir'))).toBe(false)
  })
})
