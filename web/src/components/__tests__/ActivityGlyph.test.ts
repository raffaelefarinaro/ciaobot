// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ActivityGlyph, { type ActivityGlyphKind } from '../ActivityGlyph.vue'

describe('ActivityGlyph', () => {
  it('draws a distinct, decorative mark per kind', () => {
    const expected: Array<[ActivityGlyphKind, string]> = [
      ['working', ''],
      ['agent', ''],
      ['run', ''],
      ['run-failed', ''],
    ]
    for (const [kind, text] of expected) {
      const wrapper = mount(ActivityGlyph, { props: { kind } })
      expect(wrapper.classes()).toContain(`activity-glyph--${kind}`)
      expect(wrapper.attributes('aria-hidden')).toBe('true')
      expect(wrapper.text()).toBe(text)
    }

    const working = mount(ActivityGlyph, { props: { kind: 'working' } })
    expect(working.findAll('.activity-glyph-pixel')).toHaveLength(9)
    expect(working.findAll('.activity-glyph-pixel--still')).toHaveLength(5)
    expect(mount(ActivityGlyph, { props: { kind: 'run-failed' } }).find('svg').exists()).toBe(true)
    expect(mount(ActivityGlyph, { props: { kind: 'run' } }).find('svg').exists()).toBe(false)
    expect(mount(ActivityGlyph, { props: { kind: 'agent' } }).find('svg').exists()).toBe(false)
  })
})
