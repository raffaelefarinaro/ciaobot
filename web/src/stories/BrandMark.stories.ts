import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import BrandMark from '../components/BrandMark.vue'

const meta = {
  id: 'brand-mark',
  title: 'Brand and glyphs/BrandMark',
  component: BrandMark,
} satisfies Meta<typeof BrandMark>
export default meta
type Story = StoryObj<typeof meta>

// The product mark as the sidebar renders it: a fixed 30px box that presents
// the production CiaoMark as one image named after the product. BrandMark has
// no props and no branches, so this is its only state.
export const Default: Story = {
  play: async ({ canvas }) => {
    const mark = canvas.getByRole('img', { name: 'Ciaobot' })
    expect(mark.classList.contains('brand')).toBe(true)
    expect(mark.querySelector('svg.ciao-mark')).not.toBeNull()
    expect(mark.querySelectorAll('circle[fill="var(--accent)"]')).toHaveLength(2)
  },
}
