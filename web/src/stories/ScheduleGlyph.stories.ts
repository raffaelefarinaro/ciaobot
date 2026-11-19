import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ScheduleGlyph from '../components/ScheduleGlyph.vue'

const meta = {
  id: 'schedule-glyph',
  title: 'Brand and glyphs/ScheduleGlyph',
  component: ScheduleGlyph,
  render: () => ({
    components: { ScheduleGlyph },
    template: '<div style="display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm)"><ScheduleGlyph /><span>A scheduled chat</span></div>',
  }),
} satisfies Meta<typeof ScheduleGlyph>
export default meta
type Story = StoryObj<typeof meta>

// The scheduled-chat mark: the clock face drawn like TaskGlyph, beside the
// row's own text. It has no props and no branches, and the caller owns the name.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const glyph = canvasElement.querySelector('svg.schedule-glyph')
    expect(glyph?.getAttribute('aria-hidden')).toBe('true')
    expect(glyph?.getAttribute('focusable')).toBe('false')
    expect(glyph?.querySelector('circle[r="5.2"]')).not.toBeNull()
    expect(glyph?.querySelector('path[d^="M6 3.2V6"]')).not.toBeNull()
  },
}
