// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'

const FILE_CONTENT = '# Title\n\nbody text'

/** How many times the panel has fetched the file (i.e. loaded or reloaded). */
function fileFetchCount(): number {
  const mock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } }
  return mock.mock.calls.filter(c => String(c[0]).startsWith('/api/workspace-file')).length
}

describe('PinnedFilePanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function mountPanel(options: { attach?: boolean } = {}): Promise<VueWrapper> {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.startsWith('/api/workspace-file')) {
        return new Response(FILE_CONTENT, {
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
      props: { filePath: '/vault/note.md' },
      // Selection APIs only work on nodes that are in the document, so the
      // comment test needs a real attachment point.
      ...(options.attach ? { attachTo: document.body } : {}),
    })
    await flushPromises()
    await nextTick()
    return wrapper
  }

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

  it('offers memory-map navigation for markdown files', async () => {
    const wrapper = await mountPanel()

    expect(wrapper.get('button[aria-label="Open in memory map"]')).toBeTruthy()
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
})
