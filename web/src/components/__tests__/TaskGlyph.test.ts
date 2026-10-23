// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskGlyph from '../TaskGlyph.vue'

describe('TaskGlyph', () => {
  it('is a decorative svg', () => {
    const wrapper = mount(TaskGlyph)
    expect(wrapper.element.tagName.toLowerCase()).toBe('svg')
    expect(wrapper.classes()).toContain('task-glyph')
    expect(wrapper.attributes('aria-hidden')).toBe('true')
  })
})
