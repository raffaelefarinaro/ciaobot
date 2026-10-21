// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatPaneSkeleton from '../ChatPaneSkeleton.vue'

describe('ChatPaneSkeleton', () => {
  it('renders a single status region with the loading label', () => {
    const wrapper = mount(ChatPaneSkeleton)

    const regions = wrapper.findAll('[role="status"]')
    expect(regions).toHaveLength(1)
    const region = regions[0]
    expect(region.attributes('aria-label')).toBe('Loading conversation')
    expect(region.attributes('aria-live')).toBe('polite')
    expect(region.attributes('aria-busy')).toBe('true')

    // The skeleton is a placeholder, never a control: nothing in it may take
    // focus, so the keyboard order and the touch targets of the pane are
    // exactly what they are with the transcript on screen.
    expect(region.findAll('button')).toHaveLength(0)
    expect(region.findAll('a')).toHaveLength(0)
    expect(region.findAll('input, select, textarea')).toHaveLength(0)
    expect(region.findAll('[tabindex]')).toHaveLength(0)
  })
})
