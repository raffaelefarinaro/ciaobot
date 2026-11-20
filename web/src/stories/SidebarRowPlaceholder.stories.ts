import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import SidebarRow from '../components/SidebarRow.vue'

// The placeholder branch of SidebarRow: the row's own box with a shimmer bar
// where the title goes, hidden from assistive tech. SidebarListSkeleton draws
// lists of these while a sidebar list's first load is in flight, so this is the
// row's loading shape. The link row and the keyboard path are a separate slice.
const meta = {
  id: 'sidebar-row-placeholder',
  title: 'Skeletons/SidebarRow',
  component: SidebarRow,
  args: { text: 'storybook-alpha release notes', placeholder: '72%' },
} satisfies Meta<typeof SidebarRow>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  play: async ({ canvas, canvasElement }) => {
    const row = canvasElement.querySelector('.sidebar-row--placeholder')
    expect(row).not.toBeNull()
    expect(row!.getAttribute('aria-hidden')).toBe('true')
    expect(row!.hasAttribute('tabindex')).toBe(false)
    // No link, no title and no badge: only the shimmer bar is the row.
    expect(canvas.queryByRole('link')).toBeNull()
    expect(row!.textContent?.trim()).toBe('')
    expect(row!.querySelector('.sidebar-row-title')).toBeNull()
    const bar = row!.querySelector('.sidebar-row-placeholder')
    expect(bar?.getAttribute('style')).toContain('width: 72%')
  },
}

// The same placeholder row in a 390px containing block: the bar keeps its own
// width inside the row rather than widening the sidebar.
export const Narrow: Story = {
  args: { text: 'storybook-beta drafts', placeholder: '64%' },
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvasElement }) => {
    const row = canvasElement.querySelector('.sidebar-row--placeholder')
    expect(row).not.toBeNull()
    expect(row!.getAttribute('aria-hidden')).toBe('true')
    expect(row!.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(row!.querySelector('.sidebar-row-placeholder')?.getAttribute('style')).toContain('width: 64%')
  },
}
