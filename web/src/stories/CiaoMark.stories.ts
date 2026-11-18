import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import CiaoMark from '../components/CiaoMark.vue'

const meta = {
  id: 'ciao-mark',
  title: 'Brand and glyphs/CiaoMark',
  component: CiaoMark,
  // The mark fills its container, so the story gives it a sized box the way
  // BrandMark does; the artwork itself is always the production component.
  render: () => ({
    components: { CiaoMark },
    template: '<div style="width: 64px; height: 64px; color: var(--fg)"><CiaoMark /></div>',
  }),
} satisfies Meta<typeof CiaoMark>
export default meta
type Story = StoryObj<typeof meta>

// CiaoMark has no props and no branches: one decorative inline SVG whose C
// follows currentColor and whose two dots use the accent.
export const Default: Story = {
  play: async ({ canvasElement }) => {
    const mark = canvasElement.querySelector('svg.ciao-mark')
    expect(mark?.getAttribute('aria-hidden')).toBe('true')
    expect(mark?.querySelector('path')?.getAttribute('stroke')).toBe('currentColor')
    expect(mark?.querySelectorAll('circle[fill="var(--accent)"]')).toHaveLength(2)
  },
}
