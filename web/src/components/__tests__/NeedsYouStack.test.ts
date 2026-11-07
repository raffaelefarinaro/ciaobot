// @vitest-environment jsdom

/**
 * `components/NeedsYouStack.vue` — the needs-you cards of one chat.
 *
 * Several untouched sign-ins fold into one row, so a provider listing every
 * connector of the account cannot stack a card per server over the
 * transcript. A sign-in the user acted on stays out of the fold.
 */
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

const CHAT_ID = 'chat-1'

function signIn(id: string, server: string, state: ChatAction['state'] = 'open'): ChatAction {
  return {
    id,
    kind: 'mcp_auth',
    origin: 'engine',
    state,
    params: { server, provider: 'claude' },
    error: '',
    created_at: 1,
  }
}

let wrapper: VueWrapper | null = null

function mountStack(actions: ChatAction[]) {
  const pinia = createPinia()
  setActivePinia(pinia)
  wrapper = mount(NeedsYouStack, {
    props: { chatId: CHAT_ID, actions },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  apiPost.mockReset()
})

function visibleCards(w: VueWrapper) {
  return w.findAll('.needs-you-card').filter(c => (c.element as HTMLElement).style.display !== 'none')
}

describe('NeedsYouStack', () => {
  it('shows a single sign-in as its card', () => {
    const w = mountStack([signIn('a1', 'linear')])
    expect(visibleCards(w)).toHaveLength(1)
    expect(w.find('.needs-you-fold').exists()).toBe(false)
  })

  it('folds several untouched sign-ins under one row that names them', async () => {
    const w = mountStack([signIn('a1', 'linear'), signIn('a2', 'sentry'), signIn('a3', 'notion')])
    expect(visibleCards(w)).toHaveLength(0)
    expect(w.find('.needs-you-fold-title').text()).toBe('3 connectors need a sign-in')
    expect(w.find('.needs-you-fold-names').text()).toBe('linear, sentry, notion')

    const toggle = w.findAll('.needs-you-fold-btn')[0]
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBeUndefined()
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(document.getElementById(toggle.attributes('aria-controls') ?? '')).not.toBeNull()
    expect(visibleCards(w)).toHaveLength(3)
  })

  it('keeps a sign-in the user acted on out of the fold', () => {
    const w = mountStack([signIn('a1', 'linear', 'waiting'), signIn('a2', 'sentry'), signIn('a3', 'notion')])
    const shown = visibleCards(w)
    expect(shown).toHaveLength(1)
    expect(shown[0].attributes('data-action-id')).toBe('a1')
    expect(w.find('.needs-you-fold-title').text()).toBe('2 connectors need a sign-in')
  })

  it('keeps a card started from the fold mounted, with its focus', async () => {
    const w = mountStack([signIn('a1', 'linear'), signIn('a2', 'sentry'), signIn('a3', 'notion')])
    await w.findAll('.needs-you-fold-btn')[0].trigger('click')
    const card = w.find('[data-action-id="a1"]')
    const el = card.element as HTMLElement
    el.focus()
    await nextTick()
    // The engine moves it on: it leaves the fold but is the same element.
    await w.setProps({ actions: [signIn('a1', 'linear', 'working'), signIn('a2', 'sentry'), signIn('a3', 'notion')] })
    expect(w.find('[data-action-id="a1"]').element).toBe(el)
    expect(document.activeElement).toBe(el)
  })

  it('does not pull a folded card out from under the pointer when it takes the focus', async () => {
    const w = mountStack([signIn('a1', 'linear'), signIn('a2', 'sentry'), signIn('a3', 'notion')])
    await w.findAll('.needs-you-fold-btn')[0].trigger('click')
    ;(w.find('[data-action-id="a3"]').element as HTMLElement).focus()
    await nextTick()
    expect(w.find('.needs-you-fold-title').text()).toBe('3 connectors need a sign-in')
    expect(w.findAll('.needs-you-card').map(c => c.attributes('data-action-id'))).toEqual(['a1', 'a2', 'a3'])
  })

  it('does not fold away a card the user is on when another sign-in arrives', async () => {
    const w = mountStack([signIn('a1', 'linear')])
    ;(w.find('[data-action-id="a1"]').element as HTMLElement).focus()
    await nextTick()
    await w.setProps({ actions: [signIn('a1', 'linear'), signIn('a2', 'sentry')] })
    expect(w.find('.needs-you-fold').exists()).toBe(false)
    expect(visibleCards(w)).toHaveLength(2)
  })

  it('dismisses every folded sign-in with Not now for all, then releases the focus', async () => {
    const all = [signIn('a1', 'linear'), signIn('a2', 'sentry')]
    const w = mountStack(all)
    // Each dismissal removes its card, as the engine's answer does.
    let left = [...all]
    apiPost.mockImplementation(async (path: string) => {
      left = left.filter(a => !path.includes(a.id))
      await w.setProps({ actions: left })
      return { actions: [] }
    })
    const button = w.findAll('.needs-you-fold-btn')[1]
    ;(button.element as HTMLElement).focus()
    ;(button.element as HTMLButtonElement).click()
    // The focus moved to the row before the button was disabled.
    expect(document.activeElement?.classList.contains('needs-you-fold')).toBe(true)
    await flushPromises()

    const paths = apiPost.mock.calls.map(c => String(c[0]))
    expect(paths).toHaveLength(2)
    expect(paths.every(p => p.endsWith('/dismiss'))).toBe(true)
    expect(w.find('.needs-you-fold').exists()).toBe(false)
    expect(w.emitted('releaseFocus')).toHaveLength(1)
  })
})
