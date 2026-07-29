// @vitest-environment jsdom

import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import ConfirmDialog from '../ConfirmDialog.vue'
import { askConfirm, pendingConfirm } from '../../lib/confirm'

describe('ConfirmDialog', () => {
  afterEach(() => {
    pendingConfirm.value?.resolve(false)
  })

  it('renders a themed secondary cancel action and primary confirmation', async () => {
    const wrapper = mount(ConfirmDialog, { attachTo: document.body })
    const answer = askConfirm('Archive this chat?', {
      title: 'Archive chat',
      confirmLabel: 'Archive',
    })
    await nextTick()
    await nextTick()

    const cancel = wrapper.get('.confirm-action--cancel')
    const confirm = wrapper.get('.confirm-action--primary')
    expect(cancel.text()).toBe('Cancel')
    expect(confirm.text()).toBe('Archive')
    expect(document.activeElement).toBe(cancel.element)

    await cancel.trigger('click')
    await expect(answer).resolves.toBe(false)
    wrapper.unmount()
  })

  it('uses the danger treatment for destructive confirmation', async () => {
    const wrapper = mount(ConfirmDialog)
    const answer = askConfirm('Delete this?', {
      confirmLabel: 'Delete',
      destructive: true,
    })
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.confirm-action--danger').exists()).toBe(true)
    expect(wrapper.find('.confirm-action--primary').exists()).toBe(false)

    await wrapper.get('.confirm-action--danger').trigger('click')
    await expect(answer).resolves.toBe(true)
    wrapper.unmount()
  })
})
