// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useModalOpen } from '../useModalOpen'

const Harness = defineComponent({
  setup() {
    const open = useModalOpen()
    return () => h('div', { class: 'state' }, String(open.value))
  },
})

describe('useModalOpen', () => {
  it('flips on add, remove and aria-modal toggling', async () => {
    const wrapper = mount(Harness)
    expect(wrapper.text()).toBe('false')

    const dialog = document.createElement('div')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)
    await flushPromises()
    expect(wrapper.text()).toBe('true')

    dialog.setAttribute('aria-modal', 'false')
    await flushPromises()
    expect(wrapper.text()).toBe('false')

    dialog.setAttribute('aria-modal', 'true')
    await flushPromises()
    expect(wrapper.text()).toBe('true')

    dialog.remove()
    await flushPromises()
    expect(wrapper.text()).toBe('false')
    wrapper.unmount()
  })
})
