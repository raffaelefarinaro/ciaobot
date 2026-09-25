// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CommentComposePopover from '../CommentComposePopover.vue'

const FULL_HEIGHT = 932
const KEYBOARD_HEIGHT = 596

type FakeViewport = EventTarget & { height: number; width: number }

/** One shared fake so listeners installed by an earlier mount keep working. */
const vv = new EventTarget() as FakeViewport
vv.height = FULL_HEIGHT
vv.width = 430
Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true })

function setViewportHeight(height: number): void {
  vv.height = height
  vv.dispatchEvent(new Event('resize'))
}

// These mount into document.body via Teleport, so a test that throws before its
// unmount would leave a second .compose behind and fail the next one for the
// wrong reason.
enableAutoUnmount(afterEach)

function composeTop(): number {
  const el = document.body.querySelector('.compose') as HTMLElement
  return parseFloat(el.style.top)
}

function mountCompose(props: { anchor?: { top: number; left: number } | null; modelValue?: string; images?: string[] } = {}) {
  return mount(CommentComposePopover, {
    props: {
      anchor: props.anchor === undefined ? { top: 40, left: 80 } : props.anchor,
      modelValue: props.modelValue ?? '',
      images: props.images ?? [],
    },
    attachTo: document.body,
  })
}

describe('CommentComposePopover', () => {
  beforeEach(() => {
    setViewportHeight(FULL_HEIGHT)
    vi.unstubAllGlobals()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders without a selection quote', async () => {
    const wrapper = mountCompose({ modelValue: 'I did, close' })
    await nextTick()

    expect(document.body.textContent).toContain('Add comment')
    expect(document.body.textContent).toContain('Cancel')
    expect(document.body.textContent).not.toMatch(/^"/)
    expect(document.body.querySelector('.compose-input')).not.toBeNull()
    wrapper.unmount()
  })

  it('exposes dialog semantics and keeps keyboard focus in the composer', async () => {
    const wrapper = mountCompose({ modelValue: 'note' })
    await nextTick()

    const dialog = document.body.querySelector<HTMLElement>('.compose')
    const input = dialog?.querySelector<HTMLTextAreaElement>('.compose-input')
    const save = dialog?.querySelector<HTMLButtonElement>('.compose-btn.primary')
    expect(dialog?.getAttribute('role')).toBe('dialog')
    expect(dialog?.getAttribute('aria-label')).toBe('Add comment')
    expect(document.activeElement).toBe(input)

    save?.focus()
    save?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }))
    expect(document.activeElement).toBe(input)
    wrapper.unmount()
  })

  it('emits save on Cmd+Enter and cancel on Escape', async () => {
    const wrapper = mountCompose({ anchor: { top: 10, left: 10 }, modelValue: 'note' })
    await nextTick()

    const input = document.body.querySelector('.compose-input') as HTMLTextAreaElement
    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }))
    expect(wrapper.emitted('save')).toBeTruthy()

    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('cancel')).toBeTruthy()
    wrapper.unmount()
  })

  it('cancels on Escape from an action button', async () => {
    const wrapper = mountCompose({ modelValue: 'note' })
    await nextTick()
    const cancel = document.body.querySelector<HTMLButtonElement>('.compose-btn')
    cancel?.focus()
    cancel?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))

    expect(wrapper.emitted('cancel')).toHaveLength(1)
    wrapper.unmount()
  })

  // The popover focuses its textarea on open, so on a phone the keyboard comes
  // up right after it is placed. It has to move up out of the way: it is
  // position: fixed, so anything the keyboard covers is unreachable.
  it('re-clamps above the keyboard when the viewport shrinks after opening', async () => {
    const wrapper = mountCompose({ anchor: { top: 700, left: 20 }, modelValue: 'note' })
    await nextTick()
    expect(composeTop()).toBe(700)

    setViewportHeight(KEYBOARD_HEIGHT)
    await nextTick()
    expect(composeTop()).toBeLessThanOrEqual(KEYBOARD_HEIGHT - 8)

    // And drops back when the keyboard goes away.
    setViewportHeight(FULL_HEIGHT)
    await nextTick()
    expect(composeTop()).toBe(700)
    wrapper.unmount()
  })

  it('remeasures the rendered composer instead of relying on the fallback height', async () => {
    vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(300)
    const wrapper = mountCompose({ anchor: null, modelValue: 'note' })
    await wrapper.setProps({ anchor: { top: 700, left: 20 } })
    await nextTick()
    await new Promise<void>(resolve => setTimeout(resolve, 0))

    expect(composeTop()).toBe(FULL_HEIGHT - 300)
    wrapper.unmount()
  })

  it('leaves a popover that the keyboard does not reach where it is', async () => {
    const wrapper = mountCompose({ anchor: { top: 60, left: 20 }, modelValue: 'note' })
    await nextTick()

    setViewportHeight(KEYBOARD_HEIGHT)
    await nextTick()
    expect(composeTop()).toBe(60)
    wrapper.unmount()
  })

  it('does not render when anchor is null', async () => {
    const wrapper = mountCompose({ anchor: null, modelValue: '' })
    await nextTick()
    expect(document.body.querySelector('.compose')).toBeNull()
    wrapper.unmount()
  })


})
