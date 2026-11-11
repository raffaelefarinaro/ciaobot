// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'

vi.mock('../FolderBrowser.vue', async () => {
  const { defineComponent, h } = await import('vue')
  return {
    __esModule: true,
    default: defineComponent({
      name: 'FolderBrowserStub',
      props: ['initialPath', 'chatId'],
      emits: ['navigate', 'open-file'],
      setup: () => () => h('div', { class: 'folder-browser-stub' }),
    }),
  }
})

const page = vi.hoisted(() => ({ loopback: true }))
vi.mock('../../lib/loopback', () => ({ isLoopbackPage: () => page.loopback }))

const FILE_CONTENT = '# Title\n\nbody text'

/** How many times the panel has fetched the file (i.e. loaded or reloaded). */
function fileFetchCount(): number {
  const mock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } }
  return mock.mock.calls.filter(c => String(c[0]).startsWith('/api/workspace-file')).length
}

describe('PinnedFilePanel', () => {
  beforeEach(() => {
    // The store rehydrates pending comments from localStorage on init, so a
    // fresh pinia alone does not isolate tests that stage one.
    localStorage.clear()
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    page.loopback = true
  })

  async function mountPanel(options: { attach?: boolean; filePath?: string; content?: string } = {}): Promise<VueWrapper> {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/workspace-file')) {
        return new Response(options.content ?? FILE_CONTENT, {
          status: 200,
          headers: { 'content-type': 'text/plain' },
        })
      }
      if (url.startsWith('/api/vault-markdown-paths')) {
        return new Response(JSON.stringify({ paths: [] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      return new Response('{}', { status: 404 })
    }))

    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    store.streaming = {}
    store.projectStreaming = {}
    store.bootstrapped = true

    const { default: PinnedFilePanel } = await import('../PinnedFilePanel.vue')
    const wrapper = mount(PinnedFilePanel, {
      props: { filePath: options.filePath ?? '/vault/note.md' },
      // Selection APIs only work on nodes that are in the document, so the
      // comment test needs a real attachment point.
      ...(options.attach ? { attachTo: document.body } : {}),
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  it('uses a non-stealing focus scope for the pinned read popover', async () => {
    const wrapper = await mountPanel({ attach: true })
    const store = useProjectStore()
    store.addPendingComment({
      path: '/vault/note.md', selection: 'Title', comment: 'A pinned note',
    })
    await nextTick()
    await flushPromises()

    const highlight = wrapper.get<HTMLElement>('.comment-highlight')
    highlight.element.tabIndex = 0
    highlight.element.focus()
    highlight.element.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    const popup = document.body.querySelector<HTMLElement>('.pfp-comment-pop')
    expect(popup?.getAttribute('role')).toBe('dialog')
    expect(popup?.getAttribute('aria-label')).toBe('Comment')
    expect(document.activeElement).toBe(popup?.querySelector('button'))

    popup!.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(document.body.querySelector('.pfp-comment-pop')).toBeNull()
    wrapper.unmount()
  })

  it('keeps an in-progress text edit when the model turn ends', async () => {
    const wrapper = await mountPanel()

    await wrapper.get('button[aria-label="Edit"]').trigger('click')
    await nextTick()
    const textarea = wrapper.get('textarea.pfp-edit-textarea')
    expect((textarea.element as HTMLTextAreaElement).value).toContain('# Title')

    await textarea.setValue(`${FILE_CONTENT}\n\nmy edit while streaming`)
    expect((wrapper.get('textarea.pfp-edit-textarea').element as HTMLTextAreaElement).value).toContain('my edit while streaming')

    // The model's turn starts and finishes. Before the fix the stream-end
    // auto-reload wiped the edit session; it must survive now.
    const store = useProjectStore()
    store.streaming = { 'chat-1': true }
    await nextTick()
    store.streaming = { 'chat-1': false }
    await nextTick()
    await flushPromises()

    const stillEditing = wrapper.get('textarea.pfp-edit-textarea')
    expect((stillEditing.element as HTMLTextAreaElement).value).toContain('my edit while streaming')

    // The skipped reload is not dropped, just deferred: cancelling the edit
    // releases it, so the panel stops showing a version the model moved past.
    const reloadsBefore = fileFetchCount()
    const cancel = wrapper.findAll('button').find(b => b.text() === 'Cancel')!
    await cancel.trigger('click')
    await nextTick()
    await flushPromises()
    expect(wrapper.find('textarea.pfp-edit-textarea').exists()).toBe(false)
    expect(fileFetchCount()).toBe(reloadsBefore + 1)
    wrapper.unmount()
  })

  it('stages a comment while the model is working and keeps it past stream end', async () => {
    const wrapper = await mountPanel({ attach: true })
    const store = useProjectStore()

    // The model is mid-turn: commenting used to be blocked outright here.
    store.streaming = { 'chat-1': true }
    await nextTick()

    // jsdom reports every rect as 0×0, which the selection-anchor math reads as
    // "off-screen" and refuses to place the trigger for. Give it real geometry.
    const rect = {
      top: 0, left: 0, bottom: 20, right: 100, width: 100, height: 20, x: 0, y: 0,
      toJSON: () => ({}),
    } as DOMRect
    const elementRects = vi
      .spyOn(Element.prototype, 'getBoundingClientRect')
      .mockReturnValue(rect)
    // jsdom's Range has no getClientRects at all, so define rather than spy.
    const rangeProto = Range.prototype as unknown as Record<string, unknown>
    rangeProto.getClientRects = () => [rect] as unknown as DOMRectList

    const target = wrapper.get('.pfp-md').element
    const range = document.createRange()
    range.selectNodeContents(target)
    const selection = window.getSelection()!
    selection.removeAllRanges()
    selection.addRange(range)
    document.dispatchEvent(new Event('selectionchange'))
    await nextTick()

    await wrapper.get('button.pfp-comment-trigger').trigger('click')
    await nextTick()
    // The composer is teleported to <body>, so it is queried off the document.
    const note = document.querySelector('.compose .compose-input') as HTMLTextAreaElement
    expect(note).not.toBeNull()
    note.value = 'please rename this heading'
    note.dispatchEvent(new Event('input'))
    await nextTick()

    // The turn ends: the auto-reload must not throw the open draft away.
    store.streaming = { 'chat-1': false }
    await nextTick()
    await flushPromises()
    expect(document.querySelector('.compose .compose-input')).not.toBeNull()

    const save = document.querySelector('.compose .compose-btn.primary') as HTMLButtonElement
    save.click()
    await nextTick()
    expect(store.pendingComments.map(c => c.comment)).toContain('please rename this heading')
    elementRects.mockRestore()
    delete rangeProto.getClientRects
    wrapper.unmount()
  })

  it('still reloads the file on stream end when not editing', async () => {
    const wrapper = await mountPanel()

    const store = useProjectStore()
    store.streaming = { 'chat-1': true }
    await nextTick()
    store.streaming = { 'chat-1': false }
    await nextTick()
    await flushPromises()

    // Not editing: the panel returns to the read-only preview after reload.
    expect(wrapper.find('textarea.pfp-edit-textarea').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="Edit"]').exists()).toBe(true)
    wrapper.unmount()
  })

  // ── Artifact comments ─────────────────────────────────────────────
  // The frame is an opaque origin, so the panel's whole side of this feature
  // is the postMessage relay: it must push highlights when the bridge says it
  // is ready (nothing else can reach a freshly loaded frame) and must not
  // leave an abandoned draft mark behind.
  async function mountArtifactPanel() {
    const wrapper = await mountPanel({ filePath: '/vault/dashboard.html' })
    const { default: HtmlArtifactViewer } = await import('../HtmlArtifactViewer.vue')
    // The viewer is an async component: give the resolved chunk a chance to
    // render before querying for the frame.
    for (let i = 0; i < 4; i++) {
      await flushPromises()
      await nextTick()
    }
    const viewer = wrapper.findComponent(HtmlArtifactViewer)
    expect(viewer.exists()).toBe(true)
    const postMessage = vi.fn()
    Object.defineProperty(viewer.get('iframe').element, 'contentWindow', {
      value: { postMessage } as unknown as Window,
      configurable: true,
    })
    return { wrapper, viewer, postMessage }
  }

  /** Comments from the most recent apply-comments push into the frame. */
  function lastPush(postMessage: ReturnType<typeof vi.fn>): unknown[] | null {
    const calls = postMessage.mock.calls.filter(
      c => (c[0] as { type?: string })?.type === 'ciao:apply-comments',
    )
    if (!calls.length) return null
    return (calls[calls.length - 1][0] as { comments: unknown[] }).comments
  }

  it('pushes stored artifact highlights when the bridge reports ready', async () => {
    const { wrapper, viewer, postMessage } = await mountArtifactPanel()

    // Before the handshake nothing has reached the frame: a push here would
    // hit a document that is still loading and be dropped.
    expect(lastPush(postMessage)).toBeNull()

    useProjectStore().addPendingComment({
      path: '/vault/dashboard.html',
      selection: 'Revenue rose',
      comment: 'check this',
      artifactSelector: 'body > h2:nth-of-type(1)',
      artifactStartOffset: 0,
      artifactEndOffset: 12,
      artifactElementTag: 'h2',
    })
    await nextTick()
    await nextTick()
    const beforeReady = postMessage.mock.calls.length

    viewer.vm.$emit('bridge-ready')
    await nextTick()
    await nextTick()

    // The handshake itself must produce a push, not just the list watcher:
    // on a real load the list has not changed, so `ready` is the only cue.
    expect(postMessage.mock.calls.length).toBeGreaterThan(beforeReady)
    expect(lastPush(postMessage)).toEqual([
      {
        id: expect.any(String),
        selector: 'body > h2:nth-of-type(1)',
        quote: 'Revenue rose',
        startOffset: 0,
        endOffset: 12,
        wholeElement: false,
      },
    ])
    wrapper.unmount()
  })

  it('drops the draft highlight when an artifact comment is cancelled', async () => {
    const { wrapper, viewer, postMessage } = await mountArtifactPanel()
    viewer.vm.$emit('bridge-ready')
    await nextTick()
    await nextTick()

    viewer.vm.$emit('compose-comment', {
      selector: 'body > h2:nth-of-type(1)',
      quote: 'Revenue rose',
      startOffset: 0,
      endOffset: 12,
      elementTag: 'h2',
      frameX: 10,
      frameY: 20,
    })
    await nextTick()
    await nextTick()
    expect(lastPush(postMessage)).toHaveLength(1)

    const cancel = document.querySelector('.compose .compose-btn:not(.primary)') as HTMLButtonElement
    expect(cancel).not.toBeNull()
    cancel.click()
    await nextTick()
    await nextTick()

    // The abandoned draft mark used to survive here and be re-sent forever,
    // inert on click because its id matches no stored comment.
    expect(lastPush(postMessage)).toEqual([])
    expect(useProjectStore().pendingComments).toHaveLength(0)
    wrapper.unmount()
  })

  // A selection inside the frame gets the same floating Comment button as a
  // Markdown selection (the bridge used to draw its own pill), and typing with
  // it opens the composer (keys typed into the frame never reach the page's
  // type-to-comment listener, so the bridge forwards them).
  const FRAME_SELECTION = {
    selector: 'body > table:nth-of-type(1) > thead:nth-of-type(1) > tr:nth-of-type(1)',
    quote: 'Name Owner Status',
    startOffset: 9,
    endOffset: 60,
    elementTag: 'tr',
    rect: { top: 20, left: 30, bottom: 40, right: 120 },
  }

  function placeFrame(viewer: { get: (s: string) => { element: Element } }): void {
    const frameBox = { top: 100, left: 50, bottom: 700, right: 650, width: 600, height: 600, x: 50, y: 100, toJSON: () => ({}) }
    viewer.get('iframe').element.getBoundingClientRect = () => frameBox as DOMRect
  }

  it('shows the Markdown Comment button for a selection in the artifact frame', async () => {
    const { wrapper, viewer, postMessage } = await mountArtifactPanel()
    placeFrame(viewer)
    viewer.vm.$emit('bridge-ready')
    expect(wrapper.find('.pfp-comment-trigger').exists()).toBe(false)

    viewer.vm.$emit('selection', FRAME_SELECTION)
    await nextTick()
    const trigger = wrapper.get('.pfp-comment-trigger')
    expect(trigger.attributes('title')).toBe('Comment on this selection')
    expect(trigger.text()).toBe('Comment')

    await trigger.trigger('click')
    await nextTick()
    await nextTick()

    expect(wrapper.find('.pfp-comment-trigger').exists()).toBe(false)
    expect(document.querySelector('.compose textarea')).not.toBeNull()
    // The draft is highlighted in the frame and the native selection dropped.
    expect(lastPush(postMessage)).toEqual([expect.objectContaining({ selector: FRAME_SELECTION.selector, quote: 'Name Owner Status' })])
    expect(postMessage).toHaveBeenCalledWith({ frame: 'ciao-artifact', type: 'ciao:clear-selection' }, '*')

    viewer.vm.$emit('selection-cleared')
    await nextTick()
    ;(document.querySelector('.compose .compose-btn:not(.primary)') as HTMLButtonElement).click()
    await nextTick()
    wrapper.unmount()
  })

  it('opens the composer seeded with a key typed in the frame over a selection', async () => {
    const { wrapper, viewer } = await mountArtifactPanel()
    placeFrame(viewer)
    viewer.vm.$emit('bridge-ready')

    // No selection reported: a forwarded key does nothing.
    viewer.vm.$emit('type-comment', { text: 'x' })
    await nextTick()
    expect(document.querySelector('.compose textarea')).toBeNull()

    viewer.vm.$emit('selection', FRAME_SELECTION)
    await nextTick()
    viewer.vm.$emit('type-comment', { text: 'R' })
    await nextTick()
    await nextTick()
    await flushPromises()

    const textarea = document.querySelector('.compose textarea') as HTMLTextAreaElement
    expect(textarea).not.toBeNull()
    expect(textarea.value).toBe('R')
    expect(document.activeElement).toBe(textarea)

    textarea.value = 'Rename this column'
    textarea.dispatchEvent(new Event('input'))
    ;(document.querySelector('.compose .compose-btn.primary') as HTMLButtonElement).click()
    await nextTick()
    expect(useProjectStore().pendingComments).toEqual([
      expect.objectContaining({
        selection: 'Name Owner Status',
        comment: 'Rename this column',
        artifactSelector: FRAME_SELECTION.selector,
        artifactStartOffset: 9,
        artifactEndOffset: 60,
      }),
    ])
    wrapper.unmount()
  })

  it('marks the frame busy while a composer is open and keeps a typing burst', async () => {
    const { wrapper, viewer, postMessage } = await mountArtifactPanel()
    placeFrame(viewer)
    viewer.vm.$emit('bridge-ready')
    const busyCalls = () => postMessage.mock.calls
      .map(c => c[0] as { type?: string; busy?: boolean })
      .filter(m => m.type === 'ciao:comments-busy')
      .map(m => m.busy)

    viewer.vm.$emit('selection', FRAME_SELECTION)
    await nextTick()
    // Two keys forwarded before the composer's textarea took focus.
    ;(document.activeElement as HTMLElement | null)?.blur?.()
    viewer.vm.$emit('type-comment', { text: 'R' })
    viewer.vm.$emit('type-comment', { text: 'e' })
    await nextTick()
    expect(busyCalls()).toEqual([true])
    await flushPromises()
    const textarea = document.querySelector('.compose textarea') as HTMLTextAreaElement
    expect(textarea.value).toBe('Re')

    // Once the composer has focus, a stray forwarded key is not appended.
    expect(document.activeElement).toBe(textarea)
    viewer.vm.$emit('type-comment', { text: 'x' })
    await nextTick()
    expect(textarea.value).toBe('Re')

    ;(document.querySelector('.compose .compose-btn:not(.primary)') as HTMLButtonElement).click()
    await nextTick()
    expect(busyCalls()).toEqual([true, false])
    wrapper.unmount()
  })

  it('shows document properties without repeating the heading or vault paths', async () => {
    const content = [
      '---',
      'type: project',
      'title: "LV order capture"',
      'description: "Read the order number off a printout."',
      'aliases:',
      '  - LV OCR',
      '  - order number OCR',
      'related:',
      '  - "work/memory-vault/projects/active/q4/lvmh-p6-ocr-trials.md"',
      '---',
      '',
      '# LV order capture',
      '',
      'body',
    ].join('\n')
    const wrapper = await mountPanel({ content })

    const meta = wrapper.get('.pfp-meta')
    // The title repeats the first heading, so the properties leave it out.
    expect(meta.find('.pfp-meta-name').exists()).toBe(false)
    expect(meta.get('.pfp-meta-summary').text()).toBe('Read the order number off a printout.')
    expect(meta.text()).toContain('LV OCR, order number OCR')
    expect(meta.text()).toContain('lvmh-p6-ocr-trials')
    expect(meta.text()).not.toContain('work/memory-vault')
    wrapper.unmount()
  })

  it('keeps file utilities in one menu, with memory-map navigation for markdown', async () => {
    const wrapper = await mountPanel({ attach: true })

    // The header is Edit, the menu and close - not a row of utility icons.
    expect(wrapper.find('button[aria-label="Download"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="Refresh"]').exists()).toBe(false)
    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const items = Array.from(document.querySelectorAll('.pfp-actions-menu button')).map(b => b.textContent?.trim())
    expect(items).toEqual(['Refresh', 'Copy path', 'Show in folder', 'Download', 'Open in default app', 'Open in memory map'])
    wrapper.unmount()
  })

  it('Show in folder opens the pinned file\'s folder in the viewer', async () => {
    const { useFileViewerStore } = await import('../../stores/fileViewer')
    const wrapper = await mountPanel({ attach: true })
    const openFolder = vi.spyOn(useFileViewerStore(), 'openFolder').mockResolvedValue(true)

    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const item = Array.from(document.querySelectorAll<HTMLElement>('.pfp-actions-menu button'))
      .find(b => b.textContent?.trim() === 'Show in folder')!
    item.click()
    await flushPromises()

    expect(openFolder).toHaveBeenCalledWith('/vault', 'chat-1')
    wrapper.unmount()
  })

  it('hides Open in default app away from the engine machine', async () => {
    page.loopback = false
    const wrapper = await mountPanel({ attach: true })

    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const items = Array.from(document.querySelectorAll('.pfp-actions-menu button')).map(b => b.textContent?.trim())
    expect(items).not.toContain('Open in default app')
    wrapper.unmount()
  })

  it('edits the newly pinned file after the pinned path changes', async () => {
    // Regression: switching pinned files used to leave the panel showing the
    // previous file's content when entering edit mode.
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('fileA.md')) {
        return new Response('# File A content', { status: 200, headers: { 'content-type': 'text/plain' } })
      }
      if (url.includes('fileB.md')) {
        return new Response('# File B content', { status: 200, headers: { 'content-type': 'text/plain' } })
      }
      if (url.startsWith('/api/vault-markdown-paths')) {
        return new Response(JSON.stringify({ paths: [] }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      return new Response('{}', { status: 404 })
    }))

    const store = useProjectStore()
    store.activeChatId = 'chat-1'
    store.streaming = {}
    store.bootstrapped = true

    const { default: PinnedFilePanel } = await import('../PinnedFilePanel.vue')
    const wrapper = mount(PinnedFilePanel, {
      props: { filePath: '/vault/fileA.md' },
    })
    await flushPromises()
    await nextTick()

    await wrapper.get('button[aria-label="Edit"]').trigger('click')
    await nextTick()
    expect((wrapper.get('textarea.pfp-edit-textarea').element as HTMLTextAreaElement).value).toContain('File A')

    const cancel = wrapper.findAll('button').find(b => b.text() === 'Cancel')!
    await cancel.trigger('click')
    await nextTick()
    expect(wrapper.find('textarea.pfp-edit-textarea').exists()).toBe(false)

    await wrapper.setProps({ filePath: '/vault/fileB.md' })
    await flushPromises()
    await nextTick()

    await wrapper.get('button[aria-label="Edit"]').trigger('click')
    await nextTick()
    const textarea = wrapper.get('textarea.pfp-edit-textarea')
    expect((textarea.element as HTMLTextAreaElement).value).toContain('File B')
    expect((textarea.element as HTMLTextAreaElement).value).not.toContain('File A')

    wrapper.unmount()
  })

  it('shows why an image failed to load instead of a blank box', async () => {
    const wrapper = await mountPanel({ filePath: '/ws/pic.png' })
    await wrapper.get('img.pfp-img').trigger('error')
    await flushPromises()
    expect(wrapper.get('.pfp-error').text()).toBe('File not found.')
    expect(wrapper.find('img.pfp-img').exists()).toBe(false)
    wrapper.unmount()
  })

  it('routes a folder link inside the pinned note through openLink without a chat', async () => {
    const { useFileViewerStore } = await import('../../stores/fileViewer')
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/notes/today.md', content: '[out](../exports/x)' })
    const openLink = vi.spyOn(useFileViewerStore(), 'openLink').mockResolvedValue(true)
    await wrapper.get<HTMLAnchorElement>('.pfp-md a.file-link').trigger('click')
    expect(openLink).toHaveBeenCalledWith('/ws/exports/x', null)
    expect(openLink.mock.calls[0]?.length).toBe(2)
    wrapper.unmount()
  })

  it('renders a folder pin as the folder browser with folder actions', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()

    const fb = wrapper.findComponent({ name: 'FolderBrowserStub' })
    expect(fb.props()).toEqual({ initialPath: '/ws/exports', chatId: 'chat-1' })
    expect(wrapper.get('.chat-title').text()).toBe('exports')
    expect(wrapper.get('.pfp-body').classes()).toContain('pfp-body-folder')
    expect(fileFetchCount()).toBe(0)
    expect(wrapper.find('button[aria-label="Edit"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="Unpin folder"]').exists()).toBe(true)

    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const items = Array.from(document.querySelectorAll('.pfp-actions-menu button')).map(b => b.textContent?.trim())
    expect(items).toEqual(['Refresh', 'Copy path', 'Open in default app'])
    wrapper.unmount()
  })

  it('treats a folder with a file-like name as a folder', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/notes.md/' })
    await flushPromises()

    expect(wrapper.find('.pfp-body-csv').exists()).toBe(false)
    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    const items = Array.from(document.querySelectorAll('.pfp-actions-menu button')).map(b => b.textContent?.trim())
    expect(items).not.toContain('Open in memory map')
    wrapper.unmount()
  })

  it('retitles the tile as the docked browser moves, and Refresh reloads that folder', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()

    wrapper.findComponent({ name: 'FolderBrowserStub' }).vm.$emit('navigate', '/ws/exports/sub')
    await flushPromises()
    expect(wrapper.get('.chat-title').text()).toBe('sub')
    expect(wrapper.get('.chat-title').attributes('title')).toBe('/ws/exports/sub')

    await wrapper.get('button[aria-label="More file actions"]').trigger('keydown', { key: 'Enter' })
    await flushPromises()
    Array.from(document.querySelectorAll<HTMLElement>('.pfp-actions-menu button'))
      .find(b => b.textContent?.trim() === 'Refresh')!
      .click()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'FolderBrowserStub' }).props('initialPath')).toBe('/ws/exports/sub')
    wrapper.unmount()
  })

  it('resets to the pinned folder when it is opened again', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()

    wrapper.findComponent({ name: 'FolderBrowserStub' }).vm.$emit('navigate', '/ws/exports/sub')
    await flushPromises()
    expect(wrapper.get('.chat-title').text()).toBe('sub')

    useFileViewerStore().folderRedock = { path: '/ws/exports/', seq: 1 }
    await flushPromises()
    expect(wrapper.get('.chat-title').text()).toBe('exports')
    expect(wrapper.findComponent({ name: 'FolderBrowserStub' }).props('initialPath')).toBe('/ws/exports')
    wrapper.unmount()
  })

  it('docks a file picked in the browser and offers Back to the folder', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()
    const store = useFileViewerStore()
    const openFromFolder = vi.spyOn(store, 'openFromFolder').mockResolvedValue(true)

    wrapper.findComponent({ name: 'FolderBrowserStub' }).vm.$emit('open-file', '/ws/exports/a.md')
    await flushPromises()
    expect(openFromFolder).toHaveBeenCalledWith('/ws/exports/a.md', 'chat-1', '/ws/exports')

    await wrapper.setProps({ filePath: '/ws/exports/a.md' })
    await flushPromises()
    const back = wrapper.get('.pfp-back-btn')
    expect(back.attributes('aria-label')).toBe('Back to exports')

    const openFolder = vi.spyOn(store, 'openFolder').mockResolvedValue(true)
    await back.trigger('click')
    expect(openFolder).toHaveBeenCalledWith('/ws/exports', 'chat-1')

    await wrapper.setProps({ filePath: '/ws/exports/' })
    await flushPromises()
    expect(wrapper.find('.pfp-back-btn').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows no Back when a different file is pinned after an open', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()
    vi.spyOn(useFileViewerStore(), 'openFromFolder').mockResolvedValue(true)

    wrapper.findComponent({ name: 'FolderBrowserStub' }).vm.$emit('open-file', '/ws/exports/a.md')
    await flushPromises()
    await wrapper.setProps({ filePath: '/ws/other.md' })
    await flushPromises()
    expect(wrapper.find('.pfp-back-btn').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens files the server cannot pin in the viewer', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/exports/' })
    await flushPromises()
    const openFromFolder = vi.spyOn(useFileViewerStore(), 'openFromFolder').mockResolvedValue(true)
    const fb = wrapper.findComponent({ name: 'FolderBrowserStub' })

    fb.vm.$emit('open-file', '/ws/exports/p.png')
    await flushPromises()
    expect(openFromFolder).toHaveBeenLastCalledWith('/ws/exports/p.png', '', '/ws/exports')

    fb.vm.$emit('open-file', '/ws/exports/r.docx')
    await flushPromises()
    expect(openFromFolder).toHaveBeenLastCalledWith('/ws/exports/r.docx', '', '/ws/exports')
    wrapper.unmount()
  })

  it('shows no Back for a file pinned from elsewhere', async () => {
    const wrapper = await mountPanel({ attach: true, filePath: '/ws/a.md' })
    await flushPromises()
    expect(wrapper.find('.pfp-back-btn').exists()).toBe(false)
    wrapper.unmount()
  })
})
