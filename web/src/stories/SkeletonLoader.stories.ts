import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import SkeletonLoader from '../components/SkeletonLoader.vue'

const meta = {
  id: 'skeleton-loader',
  title: 'Skeletons/SkeletonLoader',
  component: SkeletonLoader,
  args: { label: 'Loading tasks', variant: 'rows', count: 4 },
} satisfies Meta<typeof SkeletonLoader>
export default meta
type Story = StoryObj<typeof meta>

// The default shape: one bordered row per item, each with a title line and a
// short second line, all hidden from assistive tech behind the one status.
// Count is part of the args, so changing it changes the assertion.
export const Rows: Story = {
  args: { count: 4 },
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading tasks' })
    expect(status.getAttribute('aria-live')).toBe('polite')
    const rows = [...canvasElement.querySelectorAll('.skeleton-row')]
    expect(rows).toHaveLength(4)
    expect(canvasElement.querySelectorAll('.skeleton-card')).toHaveLength(0)
    expect(canvasElement.querySelector('.skeleton-graph')).toBeNull()
    for (const row of rows) {
      expect(row.getAttribute('aria-hidden')).toBe('true')
      expect(row.querySelectorAll('.skeleton-line')).toHaveLength(2)
    }
    expect(status.querySelector('.sr-only')?.textContent).toBe('Loading tasks…')
  },
}

// Cards: stacked card shapes with a heading line, a full line and a short line
// each. Three cards is the count the consuming Settings and Memory panels use.
export const Cards: Story = {
  args: { label: 'Loading memory suggestions', variant: 'cards', count: 3 },
  play: async ({ canvas, canvasElement }) => {
    expect(canvas.getByRole('status', { name: 'Loading memory suggestions' })).toBeTruthy()
    const cards = [...canvasElement.querySelectorAll('.skeleton-card')]
    expect(cards).toHaveLength(3)
    expect(canvasElement.querySelectorAll('.skeleton-row')).toHaveLength(0)
    for (const card of cards) {
      expect(card.getAttribute('aria-hidden')).toBe('true')
      expect(card.querySelector('.skeleton-line--heading')).not.toBeNull()
      expect(card.querySelectorAll('.skeleton-line')).toHaveLength(3)
    }
  },
}

// Board: four named placeholder columns, each with a heading bar and
// `max(1, count - column % 3)` cards. The per-column counts are literal on
// purpose: they are the component's own column arithmetic.
export const Board: Story = {
  args: { label: 'Loading tasks', variant: 'board', count: 3 },
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading tasks' })
    const columns = [...canvasElement.querySelectorAll('.skeleton-column')]
    expect(columns).toHaveLength(4)
    expect(columns.map((column) => column.querySelectorAll('.skeleton-card').length)).toEqual([2, 1, 3, 2])
    for (const column of columns) {
      expect(column.getAttribute('aria-hidden')).toBe('true')
      expect(column.querySelector('.skeleton-line--heading')).not.toBeNull()
    }
    expect(status.className).toContain('skeleton--board')
    // Four equal tracks while the container is wider than 940px.
    expect(getComputedStyle(status).gridTemplateColumns.split(' ')).toHaveLength(4)
  },
}

// The board inside a 390px named chat-pane container, the phone or narrow pane
// it is drawn in: the container query collapses it to one column and hides
// columns two to four, so the placeholder stays the pane's width.
export const BoardNarrow: Story = {
  args: { label: 'Loading tasks', variant: 'board', count: 3 },
  render: (args) => ({
    components: { SkeletonLoader },
    setup: () => ({ args }),
    template: '<div style="container: chat-pane / inline-size; width: 390px"><SkeletonLoader v-bind="args" /></div>',
  }),
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading tasks' })
    const columns = [...canvasElement.querySelectorAll('.skeleton-column')]
    expect(columns).toHaveLength(4)
    expect(getComputedStyle(columns[0]).display).not.toBe('none')
    for (const column of columns.slice(1)) expect(getComputedStyle(column).display).toBe('none')
    expect(getComputedStyle(status).gridTemplateColumns.split(' ')).toHaveLength(1)
    expect(status.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}

// The Memory Map's drawing: nodes and links, no rows or cards. The drawing is
// aria-hidden, so the status name is the whole accessible render.
export const Graph: Story = {
  args: { label: 'Loading the memory map', variant: 'graph', count: 4 },
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading the memory map' })
    const wrap = canvasElement.querySelector('.skeleton-graph-wrap')
    expect(wrap?.getAttribute('aria-hidden')).toBe('true')
    const svg = canvasElement.querySelector('svg.skeleton-graph')
    expect(svg?.getAttribute('viewBox')).toBe('0 0 200 140')
    expect(canvasElement.querySelectorAll('.skeleton-node')).toHaveLength(11)
    expect(canvasElement.querySelectorAll('.skeleton-edge')).toHaveLength(12)
    expect(canvasElement.querySelectorAll('.skeleton-row')).toHaveLength(0)
    expect(status.className).toContain('skeleton--graph')
  },
}

// Reduced motion is a media query, not an arg, and the browser runner has no
// reduced-motion emulation. This story renders the same production rows and
// asserts the reduced-motion rule the owner ships for them: the slow pulse that
// replaces the sweep. Flip either rule name and this play fails.
export const ReducedMotion: Story = {
  args: { count: 2 },
  play: async ({ canvas }) => {
    const styles = [...document.querySelectorAll('style')].map((element) => element.textContent ?? '')
    const ownerStyles = styles.find((text) => text.includes('@keyframes skeleton-sweep'))
    expect(ownerStyles, 'SkeletonLoader.vue stylesheets in the document').toBeDefined()
    expect(ownerStyles).toContain('@media (prefers-reduced-motion: reduce)')
    expect(ownerStyles).toContain('@keyframes skeleton-pulse')
    expect(canvas.getByRole('status', { name: 'Loading tasks' })).toBeTruthy()
  },
}
