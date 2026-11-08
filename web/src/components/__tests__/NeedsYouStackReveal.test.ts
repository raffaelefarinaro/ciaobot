// @vitest-environment jsdom

/**
 * `components/NeedsYouStack.vue` reveals the key control of a card that
 * appears or changes state, by scrolling the stack (never the page or the
 * focus). The geometry is mocked: jsdom has no layout.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import NeedsYouStack from '../NeedsYouStack.vue'
import type { ChatAction } from '../../lib/types'

const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: apiPost, patch: vi.fn(), del: vi.fn(), postForm: vi.fn() },
}))
// Off-loopback, so a waiting sign-in with a url shows its paste field.
vi.mock('../../lib/loopback', () => ({
  isLoopbackPage: () => false,
  isLoopbackHostname: () => false,
}))

const CHAT_ID = 'chat-1'
/** The stack's box is 0..300. */
const STACK = { top: 0, bottom: 300 }
let reduce = false
/** Where the key control (`[data-needs-you-primary]`) sits. */
let primary = { top: 380, bottom: 460 }
let scrollTo: ReturnType<typeof vi.fn>
// jsdom has no Element.prototype.scrollTo; the original (undefined) is put back after each test.
const originalScrollTo = (Element.prototype as { scrollTo?: unknown }).scrollTo

function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 0, x: 0, y: top, width: 0, height: bottom - top, toJSON() {} } as DOMRect
}

function signIn(id: string, state: ChatAction['state'] = 'open', server = 'linear'): ChatAction {
  return {
    id,
    kind: 'mcp_auth',
    origin: 'engine',
    state,
    params: { server, provider: 'claude', url: 'https://auth.example.invalid/a' },
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

beforeEach(() => {
  reduce = false
  primary = { top: 380, bottom: 460 }
  scrollTo = vi.fn()
  Element.prototype.scrollTo = scrollTo as unknown as typeof Element.prototype.scrollTo
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('reduce') && reduce,
    media: query,
    addEventListener() {},
    removeEventListener() {},
  }))
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this.classList.contains('needs-you-stack')) return rect(STACK.top, STACK.bottom)
    if (this.hasAttribute('data-needs-you-primary')) return rect(primary.top, primary.bottom)
    return rect(0, 0)
  })
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  apiPost.mockReset()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  ;(Element.prototype as { scrollTo?: unknown }).scrollTo = originalScrollTo
})

describe('NeedsYouStack reveal', () => {
  it('scrolls a waiting sign-in so its paste field is in view, without moving the focus', async () => {
    const w = mountStack([signIn('a1', 'open')])
    await flushPromises()
    expect(scrollTo).not.toHaveBeenCalled()

    const before = document.activeElement
    await w.setProps({ actions: [signIn('a1', 'waiting')] })
    await flushPromises()

    expect(scrollTo).toHaveBeenCalledTimes(1)
    expect(scrollTo.mock.contexts[0]).toBe(w.find('.needs-you-stack').element)
    // 460 + 8 pad - 300 bottom = 168 more to scroll.
    expect(scrollTo).toHaveBeenCalledWith({ top: 168, behavior: 'smooth' })
    expect(document.activeElement).toBe(before)
  })

  it('jumps instead of gliding under reduced motion', async () => {
    reduce = true
    const w = mountStack([signIn('a1', 'waiting')])
    await flushPromises()

    expect(scrollTo).toHaveBeenCalledWith({ top: 168, behavior: 'auto' })
    expect(scrollTo.mock.contexts[0]).toBe(w.find('.needs-you-stack').element)
  })

  it('does not scroll while the focus is in another card', async () => {
    const w = mountStack([signIn('a1', 'waiting')])
    await flushPromises()
    scrollTo.mockReset()

    const input = w.find('[data-action-id="a1"] input').element as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    await w.setProps({ actions: [signIn('a1', 'waiting'), signIn('a2', 'waiting', 'sentry')] })
    await flushPromises()

    expect(scrollTo).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(input)
  })

  it('does not scroll when the key control is already in view', async () => {
    primary = { top: 100, bottom: 200 }
    mountStack([signIn('a1', 'waiting')])
    await flushPromises()

    expect(scrollTo).not.toHaveBeenCalled()
  })
})
