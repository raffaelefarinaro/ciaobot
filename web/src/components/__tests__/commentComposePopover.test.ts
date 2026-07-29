// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import CommentComposePopover from '../CommentComposePopover.vue'

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
