// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ActivityGlyph, { type ActivityGlyphKind } from '../ActivityGlyph.vue'

describe('ActivityGlyph', () => {
  it('draws a distinct, decorative mark per kind', () => {
    const expected: Array<[ActivityGlyphKind, string]> = [
      ['working', ''],
      ['run', ''],
      ['run-failed', ''],
    ]
    for (const [kind, text] of expected) {
      const wrapper = mount(ActivityGlyph, { props: { kind } })
      expect(wrapper.classes()).toContain(`activity-glyph--${kind}`)
      expect(wrapper.attributes('aria-hidden')).toBe('true')
      expect(wrapper.text()).toBe(text)
    }

    // Every running kind is the same 3x3 grid; the reduced-motion frame
    // (the --still cells) is what tells them apart without motion.
    const stills: Array<[ActivityGlyphKind, number[]]> = [
      ['working', [0, 2, 4, 6, 8]],
      ['run', [2, 4]],
    ]
    for (const [kind, lit] of stills) {
      const pixels = mount(ActivityGlyph, { props: { kind } }).findAll('.activity-glyph-pixel')
      expect(pixels).toHaveLength(9)
      const still = pixels.flatMap((p, i) => (p.classes('activity-glyph-pixel--still') ? [i] : []))
      expect(still, kind).toEqual(lit)
    }
    expect(mount(ActivityGlyph, { props: { kind: 'run-failed' } }).find('svg').exists()).toBe(true)
    expect(mount(ActivityGlyph, { props: { kind: 'run' } }).find('svg').exists()).toBe(false)
  })
})
