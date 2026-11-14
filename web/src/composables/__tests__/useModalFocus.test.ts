// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, ref } from 'vue'
import { useModalFocus } from '../useModalFocus'

const Harness = defineComponent({
  setup() {
    const panel = ref<HTMLElement | null>(null)
    const first = ref<HTMLButtonElement | null>(null)
    const active = ref(true)
    useModalFocus(panel, active, {
      initialFocus: first,
      onEscape: () => { active.value = false },
    })
    return () => h('div', [
      h('section', { ref: panel, 'aria-label': 'Dialog' }, [
        h('button', { ref: first }, 'Confirm'),
        h('button', 'Cancel'),
      ]),
      h('main', { id: 'background' }, 'Background'),
    ])
  },
})

describe('useModalFocus', () => {
  it.each([{ isComposing: true }, { keyCode: 229 }])('does not claim IME Escape in a modal: %o', async init => {
    let escapes = 0
    const Consumer = defineComponent({
      setup() {
        const panel = ref<HTMLElement | null>(null)
        useModalFocus(panel, ref(true), { onEscape: () => { escapes++ } })
        return () => h('section', { ref: panel }, [h('textarea')])
      },
    })
    const wrapper = mount(Consumer, { attachTo: document.body })
    await flushPromises()
    const input = wrapper.get('textarea').element
    const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true, ...init })
    input.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(false)
    expect(escapes).toBe(0)
    const ordinary = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true })
    input.dispatchEvent(ordinary)
    expect(ordinary.defaultPrevented).toBe(true)
    expect(escapes).toBe(1)
    wrapper.unmount()
  })
  it('activates an initially-open surface after mount', async () => {
    const wrapper = mount(Harness, { attachTo: document.body })
    const background = wrapper.get('#background').element as HTMLElement
    await flushPromises()

    expect(document.activeElement).toBe(wrapper.get('button').element)
    expect(background.inert).toBe(true)
    expect(background.getAttribute('aria-hidden')).toBe('true')

    wrapper.unmount()
    expect(background.inert).not.toBe(true)
    expect(background.hasAttribute('aria-hidden')).toBe(false)
  })
})
