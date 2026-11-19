import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ActivityGlyph from '../components/ActivityGlyph.vue'

const meta = {
  id: 'activity-glyph',
  title: 'Brand and glyphs/ActivityGlyph',
  component: ActivityGlyph,
  args: { kind: 'working' },
  // Decorative only: the row that renders it owns its accessible name.
  render: (args) => ({
    components: { ActivityGlyph },
    setup: () => ({ args }),
    template: '<div style="display: inline-flex; align-items: center; padding: var(--space-3); color: var(--accent); font-size: var(--text-sm)"><ActivityGlyph v-bind="args" /></div>',
  }),
} satisfies Meta<typeof ActivityGlyph>
export default meta
type Story = StoryObj<typeof meta>

// A working turn: the 3x3 pixel grid, one fixed dim centre and the chasing
// perimeter. The reduced-motion frame of the same grid is a checker.
export const Working: Story = {
  play: async ({ canvasElement }) => {
    const glyph = canvasElement.querySelector('.activity-glyph--working')
    expect(glyph?.getAttribute('aria-hidden')).toBe('true')
    expect(glyph?.querySelectorAll('.activity-glyph-pixel')).toHaveLength(9)
    expect(glyph?.querySelectorAll('.activity-glyph-pixel--fixed')).toHaveLength(1)
    expect(glyph?.querySelectorAll('.activity-glyph-pixel--still')).toHaveLength(5)
  },
}

// A filled alert triangle with the knocked-out mark: still, never animated,
// for a failure or a question that wants its owner.
export const Alert: Story = {
  args: { kind: 'alert' },
  play: async ({ canvasElement }) => {
    const glyph = canvasElement.querySelector('.activity-glyph--alert')
    expect(glyph?.querySelector('path')?.getAttribute('fill')).toBe('currentColor')
    expect(glyph?.querySelectorAll('.activity-glyph-knock')).toHaveLength(2)
    expect(glyph?.querySelectorAll('.activity-glyph-pixel')).toHaveLength(0)
  },
}
