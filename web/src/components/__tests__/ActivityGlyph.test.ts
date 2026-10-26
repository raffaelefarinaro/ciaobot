// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ActivityGlyph, { type ActivityGlyphKind } from '../ActivityGlyph.vue'

describe('ActivityGlyph', () => {
  it('draws a distinct, decorative mark per kind', () => {
    const expected: Array<[ActivityGlyphKind, string]> = [
      ['working', ''],
      ['run', ''],
      ['alert', ''],
    ]
    for (const [kind, text] of expected) {
      const wrapper = mount(ActivityGlyph, { props: { kind } })
      expect(wrapper.classes()).toContain(`activity-glyph--${kind}`)
      expect(wrapper.attributes('aria-hidden')).toBe('true')
      expect(wrapper.text()).toBe(text)
    }

    // Working is the 3x3 grid; its reduced-motion frame is a checker.
    const pixels = mount(ActivityGlyph, { props: { kind: 'working' } }).findAll('.activity-glyph-pixel')
    expect(pixels).toHaveLength(9)
    const still = pixels.flatMap((p, i) => (p.classes('activity-glyph-pixel--still') ? [i] : []))
    expect(still).toEqual([0, 2, 4, 6, 8])

    // A background run is a ring with one orbiting dot, never the grid.
    const run = mount(ActivityGlyph, { props: { kind: 'run' } })
    expect(run.find('.activity-glyph-ring').exists()).toBe(true)
    expect(run.find('.activity-glyph-orbit').exists()).toBe(true)
    expect(run.findAll('.activity-glyph-pixel')).toHaveLength(0)
    expect(run.find('svg').exists()).toBe(false)
    expect(mount(ActivityGlyph, { props: { kind: 'alert' } }).find('svg').exists()).toBe(true)
  })
})
