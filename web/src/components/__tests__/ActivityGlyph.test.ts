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
  })
})
