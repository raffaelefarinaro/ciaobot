// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'

const FILE_CONTENT = '# Title\n\nbody text'

describe('PinnedFilePanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function mountPanel(): Promise<VueWrapper> {
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
})
