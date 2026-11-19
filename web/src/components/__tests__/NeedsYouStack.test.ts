// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NeedsYouStack from '../NeedsYouStack.vue'
import type { ChatAction } from '../../lib/types'

const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))

function command(id: string, state: ChatAction['state'] = 'open'): ChatAction {
  return { id, kind: 'command', origin: 'engine', state, params: { command: `echo ${id}` }, error: '', created_at: 1 }
}
let wrapper: VueWrapper | null = null
function mountStack(actions: ChatAction[]) {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(NeedsYouStack, { props: { chatId: 'chat-1', actions }, attachTo: document.body, global: { plugins: [pinia] } })
  return wrapper
}
afterEach(() => { wrapper?.unmount(); wrapper = null; apiPost.mockReset() })
function ids(w: VueWrapper) { return w.findAll('.needs-you-card').map(c => c.attributes('data-action-id')) }

describe('NeedsYouStack', () => {
  it('shows a single command', () => {
    const w = mountStack([command('a1')])
    expect(ids(w)).toEqual(['a1'])
    expect(w.text()).toContain('echo a1')
  })
  it('shows all generic cards in input order', () => {
    const w = mountStack([command('a3'), command('a1'), command('a2')])
    expect(ids(w)).toEqual(['a3', 'a1', 'a2'])
  })
  it('keeps a command card and its focus when its state changes', async () => {
    const w = mountStack([command('a1'), command('a2')])
    const el = w.find('[data-action-id="a1"]').element as HTMLElement
    el.focus()
    await w.setProps({ actions: [command('a1', 'working'), command('a2')] })
    expect(w.find('[data-action-id="a1"]').element).toBe(el)
    expect(document.activeElement).toBe(el)
  })
  it('does not reorder focused cards when another generic card arrives', async () => {
    const w = mountStack([command('a1'), command('a2')])
    ;(w.find('[data-action-id="a2"]').element as HTMLElement).focus()
    await nextTick()
    await w.setProps({ actions: [command('a1'), command('a2'), command('a3')] })
    expect(ids(w)).toEqual(['a1', 'a2', 'a3'])
  })
  it('dismisses only the selected generic card for each control, without a bulk request', async () => {
    apiPost.mockResolvedValue({ actions: [] })
    const w = mountStack([command('a1'), command('a2')])
    for (const id of ['a1', 'a2']) {
      await w.find(`[data-action-id="${id}"] .needs-you-btn--quiet`).trigger('click')
      await flushPromises()
    }
    expect(apiPost.mock.calls.map(c => c[0])).toEqual(['/api/chats/chat-1/actions/a1/dismiss', '/api/chats/chat-1/actions/a2/dismiss'])
    expect(w.text()).not.toContain('Not now for all')
  })
})
