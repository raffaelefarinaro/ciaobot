import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import TaskGlyph from '../components/TaskGlyph.vue'

const meta = {
  id: 'task-glyph',
  title: 'Brand and glyphs/TaskGlyph',
  component: TaskGlyph,
  render: () => ({
    components: { TaskGlyph },
    template: '<div style="display: inline-flex; align-items: center; gap: var(--space-2); font-size: var(--text-sm)"><TaskGlyph /><span>A task chat</span></div>',
  }),
} satisfies Meta<typeof TaskGlyph>
export default meta
type Story = StoryObj<typeof meta>

// The task-chat mark: a rounded box with a tick, drawn beside the row's own
// text. It has no props and no branches, and the caller owns the name.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const glyph = canvasElement.querySelector('svg.task-glyph')
    expect(glyph?.getAttribute('aria-hidden')).toBe('true')
    expect(glyph?.getAttribute('focusable')).toBe('false')
    expect(glyph?.querySelector('rect')).not.toBeNull()
    expect(glyph?.querySelector('path[d^="M3.4 6.2"]')).not.toBeNull()
  },
}
