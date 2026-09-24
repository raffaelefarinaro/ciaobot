// @vitest-environment jsdom

// The read popover moved out of ChatPanel into its own component so hovering a
// highlight no longer re-renders the whole transcript. ChatPanel itself is too
// heavy to mount (the smoke test stubs it), so the behaviour it used to own is
// pinned down here: hover previews, click pins, a pin surviving a stray hover,
// and the close grace period.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ChatCommentPopover from '../ChatCommentPopover.vue'

const DRAFT_ID = '__draft__'

const comments = [
  { id: 'c1', comment: 'first note' },
  { id: 'c2', comment: 'second note', images: ['img-a'] },
]

function mountPopover(list = comments) {
  return mount(ChatCommentPopover, {
    props: { comments: list, draftId: DRAFT_ID },
    attachTo: document.body,
  })
}

// Stands in for a highlight span rendered inside a chat bubble.
function highlight(id: string): HTMLElement {
  const el = document.createElement('span')
  el.className = 'comment-highlight'
  el.dataset.commentId = id
  document.body.appendChild(el)
  return el
}

function mouseEvent(el: HTMLElement, relatedTarget: Node | null = null): MouseEvent {
  const e = new MouseEvent('mouseover', { bubbles: true, relatedTarget })
  Object.defineProperty(e, 'target', { value: el, configurable: true })
  return e
}

beforeEach(() => {
  document.body.innerHTML = ''
  // The composable gates hover previews on pointer capability; jsdom has no
  // matchMedia, so declare this a hover-capable device.
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('ChatCommentPopover', () => {
  it('previews a comment on hover and renders its note', async () => {
    const wrapper = mountPopover()
    wrapper.vm.onTargetOver(mouseEvent(highlight('c1')))
    await nextTick()

    expect(wrapper.vm.openId).toBe('c1')
    expect(document.body.textContent).toContain('first note')
  })

  it('ignores the in-flight draft highlight, which has no saved comment yet', async () => {
    const wrapper = mountPopover()
    wrapper.vm.onTargetOver(mouseEvent(highlight(DRAFT_ID)))
    await nextTick()

    expect(wrapper.vm.openId).toBeNull()
  })

  it('does not open for a comment id that no longer exists', async () => {
    const wrapper = mountPopover()
    wrapper.vm.onTargetOver(mouseEvent(highlight('deleted')))
    await nextTick()

    expect(document.body.textContent).not.toContain('first note')
  })

  it('pins on click and returns the id so the sidebar can scroll to the card', async () => {
    const wrapper = mountPopover()
    const id = wrapper.vm.pinFromEvent(mouseEvent(highlight('c2')))
    await nextTick()

    expect(id).toBe('c2')
    expect(wrapper.vm.openId).toBe('c2')
    expect(document.body.textContent).toContain('second note')
  })

  it('returns null when the click was not on a highlight', () => {
    const wrapper = mountPopover()
    const plain = document.createElement('div')
    document.body.appendChild(plain)

    expect(wrapper.vm.pinFromEvent(mouseEvent(plain))).toBeNull()
  })

  it('does not let a stray hover demote a pinned popover', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountPopover()
      const el = highlight('c1')
      wrapper.vm.pinFromEvent(mouseEvent(el))
      await nextTick()

      wrapper.vm.onTargetOver(mouseEvent(el))
      wrapper.vm.onTargetOut(mouseEvent(el))
      await vi.advanceTimersByTimeAsync(400)

      expect(wrapper.vm.openId).toBe('c1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('closes a hover preview only after the grace period', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountPopover()
      const el = highlight('c1')
      wrapper.vm.onTargetOver(mouseEvent(el))
      await nextTick()
      expect(wrapper.vm.openId).toBe('c1')

      wrapper.vm.onTargetOut(mouseEvent(el))
      expect(wrapper.vm.openId).toBe('c1') // still open during the grace period
      await vi.advanceTimersByTimeAsync(200)
      expect(wrapper.vm.openId).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps the preview open when the pointer moves into the popover', async () => {
    vi.useFakeTimers()
    try {
      const wrapper = mountPopover()
      const el = highlight('c1')
      wrapper.vm.onTargetOver(mouseEvent(el))
      await nextTick()

      wrapper.vm.onTargetOut(mouseEvent(el))
      // Pointer lands on the popover itself mid-grace, via a real DOM event.
      const pop = document.querySelector('.pop')
      expect(pop).not.toBeNull()
      pop!.dispatchEvent(new MouseEvent('mouseenter', { bubbles: false }))
      await vi.advanceTimersByTimeAsync(400)

      expect(wrapper.vm.openId).toBe('c1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('gives a pinned popover dialog semantics, focuses its action, and returns focus on Escape', async () => {
    const wrapper = mountPopover()
    const el = highlight('c1')
    el.tabIndex = 0
    el.focus()
    wrapper.vm.pinFromEvent(mouseEvent(el))
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    const popup = document.querySelector<HTMLElement>('.pop')!
    expect(popup.getAttribute('role')).toBe('dialog')
    expect(popup.getAttribute('aria-label')).toBe('Comment')
    expect(document.activeElement).toBe(popup.querySelector('button'))

    popup.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(wrapper.vm.openId).toBeNull()
    expect(document.activeElement).toBe(el)
  })

  it('clamps a pinned popover to a shrinking visual viewport', async () => {
    vi.stubGlobal('visualViewport', { width: 320, height: 200 })
    const wrapper = mountPopover()
    const el = highlight('c1')
    vi.spyOn(el, 'getBoundingClientRect').mockReturnValue(new DOMRect(4, 190, 40, 12))
    wrapper.vm.pinFromEvent(mouseEvent(el))
    await nextTick()

    const popup = document.querySelector<HTMLElement>('.pop')!
    // 190 + 6 would put the 80px fallback below the 200px visible area;
    // the top is pulled up to the 200 - 80 bottom edge.
    expect(parseFloat(popup.style.top)).toBe(120)
    expect(parseFloat(popup.style.left)).toBe(8)
  })

  it('re-clamps an open popover when the visual viewport changes', async () => {
    const visualViewport = { width: 320, height: 400 }
    vi.stubGlobal('visualViewport', visualViewport)
    const wrapper = mountPopover()
    const el = highlight('c1')
    vi.spyOn(el, 'getBoundingClientRect').mockReturnValue(new DOMRect(4, 380, 40, 12))
    wrapper.vm.pinFromEvent(mouseEvent(el))
    await nextTick()
    expect(parseFloat(document.querySelector<HTMLElement>('.pop')!.style.top)).toBe(320)

    visualViewport.height = 200
    window.dispatchEvent(new Event('resize'))
    await nextTick()
    expect(parseFloat(document.querySelector<HTMLElement>('.pop')!.style.top)).toBe(120)
  })

  it('keeps a hover preview non-focus-stealing for touch-capable users', async () => {
    const wrapper = mountPopover()
    const el = highlight('c1')
    const before = document.activeElement
    wrapper.vm.onTargetOver(mouseEvent(el))
    await nextTick()

    expect(wrapper.vm.openId).toBe('c1')
    expect(document.activeElement).toBe(before)
  })


  it('close() dismisses immediately', async () => {
    const wrapper = mountPopover()
    wrapper.vm.onTargetOver(mouseEvent(highlight('c1')))
    await nextTick()

    wrapper.vm.close()
    expect(wrapper.vm.openId).toBeNull()
  })

  it('skips hover work entirely when the chat has no comments', async () => {
    const wrapper = mountPopover([])
    wrapper.vm.onTargetOver(mouseEvent(highlight('c1')))
    await nextTick()

    expect(wrapper.vm.openId).toBeNull()
  })
})
