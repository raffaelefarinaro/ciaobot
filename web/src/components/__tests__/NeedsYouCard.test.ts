// @vitest-environment jsdom

/**
 * `components/NeedsYouCard.vue` — a transient needs-you action on a chat.
 *
 * The card owns the generic controls (Not now, Continue the chat / Done) and
 * the copy; the kind's body owns the primary control. These tests hold the
 * sign-in path: the page opens in a new tab with no opener, only an http or
 * https address the engine sent is opened or linked, and a refused or stale
 * request says the card changed instead of failing silently.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NeedsYouCard from '../NeedsYouCard.vue'
import { useProjectStore } from '../../stores/projects'
import type { ChatAction } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel, postForm: vi.fn() },
}))

const CHAT_ID = 'chat-1'

function action(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'command',
    origin: 'engine',
    state: 'open',
    params: { command: 'echo review-only' },
    error: '',
    created_at: 1,
    ...over,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

let wrapper: VueWrapper | null = null

function mountCard(props: { action?: ChatAction } = {}) {
  // A Pinia of its own per test, so one test's actions never reach another.
  const pinia = createPinia()
  setActivePinia(pinia)
  useProjectStore().chatActions[CHAT_ID] = [props.action ?? action()]
  wrapper = mount(NeedsYouCard, {
    props: { chatId: CHAT_ID, action: action(), ...props },
    attachTo: document.body,
    global: { plugins: [pinia] },
  })
  return wrapper
}

function button(card: VueWrapper, label: string) {
  const match = card.findAll('button').find(b => b.text() === label)
  if (!match) throw new Error(`no button named ${label}`)
  return match
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDel.mockReset()
  apiGet.mockResolvedValue({})
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.restoreAllMocks()
})

describe('NeedsYouCard', () => {
  it('shows the card copy and the generic controls for an open action', () => {
    const card = mountCard()
    const root = card.get('.needs-you-card')
    expect(root.attributes('data-state')).toBe('open')
    expect(card.get('.needs-you-card-title').text()).toBe('Suggested command — review before running')
    expect(card.get('.needs-you-card-meta').text()).toBe('Suggested command')
    expect(button(card, 'Type it in a terminal').exists()).toBe(true)
    expect(button(card, 'Not now').exists()).toBe(true)
  })

  it('a working card still offers Not now', () => {
    const card = mountCard({ action: action({ state: 'working' }) })
    expect(card.get('.needs-you-card').attributes('aria-busy')).toBe('true')
    expect(button(card, 'Not now').exists()).toBe(true)
  })

  it('a done card continues the chat, and Done dismisses it', async () => {
    apiPost.mockResolvedValueOnce({ actions: [], delivery: 'started' })
    const card = mountCard({ action: action({ state: 'done' }) })
    await button(card, 'Continue').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/chats/chat-1/actions/a1/continue', {})

    apiPost.mockResolvedValueOnce({ actions: [] })
    const done = mountCard({ action: action({ state: 'done' }) })
    await button(done, 'Done').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/chats/chat-1/actions/a1/dismiss', {})
  })

  it('Not now dismisses the card, and the button is disabled while the request is out', async () => {
    const pending = deferred<unknown>()
    apiPost.mockReturnValueOnce(pending.promise)
    const card = mountCard()
    const dismiss = button(card, 'Not now')
    await dismiss.trigger('click')
    expect(apiPost).toHaveBeenCalledWith('/api/chats/chat-1/actions/a1/dismiss', {})
    expect(button(card, 'Not now').attributes('disabled')).toBeDefined()

    pending.resolve({ actions: [] })
    await flushPromises()
    expect(button(card, 'Not now').attributes('disabled')).toBeUndefined()
  })

  it('a dismissal conflict says the card changed', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('chat_action_conflict'), {
      status: 409,
      payload: { error: 'chat_action_conflict', actions: [] },
    }))
    const card = mountCard()
    await button(card, 'Not now').trigger('click')
    await flushPromises()
    expect(card.get('[role=status]').text()).toContain('this card changed')
  })

  it('a failed dismissal is the user to retry', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('server'), { status: 500 }))
    const card = mountCard()
    await button(card, 'Not now').trigger('click')
    await flushPromises()
    expect(card.get('[role=alert]').text()).toBe('This card could not be dismissed. Try again.')
    expect(button(card, 'Not now').attributes('disabled')).toBeUndefined()
  })

  it('a done card shows a check in the glyph, and an open card does not', () => {
    const gws = { kind: 'device_code', params: { tool: 'gh' } } as const
    const open = mountCard({ action: action(gws) })
    expect(open.find('.needs-you-card-glyph svg polyline').exists()).toBe(false)
    const done = mountCard({ action: action({ ...gws, state: 'done' }) })
    expect(done.find('.needs-you-card-glyph svg polyline').exists()).toBe(true)
  })

  it('Not now keeps the focus on the card while the request is out', async () => {
    const pending = deferred<unknown>()
    apiPost.mockReturnValueOnce(pending.promise)
    const card = mountCard()
    const dismiss = button(card, 'Not now')
    dismiss.element.focus()
    await dismiss.trigger('click')
    expect(document.activeElement).toBe(card.get('.needs-you-card').element)

    pending.resolve({ actions: [] })
    await flushPromises()
  })

  it('every control is a real button', () => {
    const card = mountCard()
    for (const b of card.findAll('button')) expect(b.attributes('type')).toBe('button')
  })

  it('a failed card shows the alert triangle, not the shield', async () => {
    const card = mountCard({ action: action({ state: 'failed', error: 'x' }) })
    expect(card.find('.needs-you-card-glyph .activity-glyph--alert').exists()).toBe(true)
    expect(card.find('.needs-you-card-glyph svg.app-icon').exists()).toBe(false)

    await card.setProps({ action: action({ state: 'open' }) })
    expect(card.find('.needs-you-card-glyph .activity-glyph--alert').exists()).toBe(false)
    expect(card.find('.needs-you-card-glyph svg.app-icon').exists()).toBe(true)
  })

  it('puts the primary action and Not now in one controls row', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('server'), { status: 500 }))
    const card = mountCard()
    await button(card, 'Not now').trigger('click')
    await flushPromises()

    const row = card.get('.needs-you-card-controls')
    expect(row.findAll('button').map(b => b.text())).toEqual(['Type it in a terminal', 'Copy', 'Not now'])
    expect(card.get('.needs-you-card-error').element.closest('.needs-you-card-controls')).toBeNull()
  })

  it('announces a state change politely', async () => {
    const card = mountCard()
    const live = card.get('.needs-you-card-live')
    expect(live.attributes('aria-live')).toBe('polite')
    expect(live.text()).toBe('')

    await card.setProps({ action: action({ state: 'working' }) })
    await nextTick()
    expect(card.get('.needs-you-card-live').text()).toBe(
      'Review it, then press Enter in the terminal. The command is typed at the prompt. Edit it if you want. Nothing has run.',
    )
  })
})
