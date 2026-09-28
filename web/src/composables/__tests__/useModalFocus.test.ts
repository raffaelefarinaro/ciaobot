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
