// @vitest-environment jsdom

import { afterEach, describe, expect, it } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'
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

describe('CommentComposePopover', () => {
  it('renders without a selection quote', async () => {
    const wrapper = mount(CommentComposePopover, {
      props: {
        anchor: { top: 40, left: 80 },
        modelValue: 'I did, close',
        images: [],
      },
      attachTo: document.body,
    })
    await nextTick()

    expect(document.body.textContent).toContain('Add comment')
    expect(document.body.textContent).toContain('Cancel')
    expect(document.body.textContent).not.toMatch(/^"/)
    expect(document.body.querySelector('.compose-input')).not.toBeNull()
    wrapper.unmount()
  })

  it('emits save on Cmd+Enter and cancel on Escape', async () => {
    const wrapper = mount(CommentComposePopover, {
      props: {
        anchor: { top: 10, left: 10 },
        modelValue: 'note',
      },
      attachTo: document.body,
    })
    await nextTick()

    const input = document.body.querySelector('.compose-input') as HTMLTextAreaElement
    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', metaKey: true, bubbles: true }))
    expect(wrapper.emitted('save')).toBeTruthy()

    await input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(wrapper.emitted('cancel')).toBeTruthy()
    wrapper.unmount()
  })

  // The popover focuses its textarea on open, so on a phone the keyboard comes
  // up right after it is placed. It has to move up out of the way: it is
  // position: fixed, so anything the keyboard covers is unreachable.
  it('re-clamps above the keyboard when the viewport shrinks after opening', async () => {
    setViewportHeight(FULL_HEIGHT)
    const wrapper = mount(CommentComposePopover, {
      props: { anchor: { top: 700, left: 20 }, modelValue: 'note' },
      attachTo: document.body,
    })
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

  it('leaves a popover that the keyboard does not reach where it is', async () => {
    setViewportHeight(FULL_HEIGHT)
    const wrapper = mount(CommentComposePopover, {
      props: { anchor: { top: 60, left: 20 }, modelValue: 'note' },
      attachTo: document.body,
    })
    await nextTick()

    setViewportHeight(KEYBOARD_HEIGHT)
    await nextTick()
    expect(composeTop()).toBe(60)
    wrapper.unmount()
  })

  it('does not render when anchor is null', async () => {
    const wrapper = mount(CommentComposePopover, {
      props: { anchor: null, modelValue: '' },
      attachTo: document.body,
    })
    await nextTick()
    expect(document.body.querySelector('.compose')).toBeNull()
    wrapper.unmount()
  })
})
