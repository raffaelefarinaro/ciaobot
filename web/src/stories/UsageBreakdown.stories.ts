import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import UsageBreakdown from '../components/UsageBreakdown.vue'
import { syntheticUsageStats } from './usageFixtures'

const narrow: Meta<typeof UsageBreakdown>['decorators'] = [
  () => ({ template: '<div style="max-width: 390px"><story /></div>' }),
]

const meta = {
  id: 'usage-breakdown',
  title: 'Usage/UsageBreakdown',
  component: UsageBreakdown,
  decorators: narrow,
  args: {
    stats: syntheticUsageStats('storybook-alpha', 30),
    rangeDays: 30,
  },
} satisfies Meta<typeof UsageBreakdown>
export default meta
type Story = StoryObj<typeof meta>

export const Loaded: Story = {
  play: async ({ canvas }) => {
    expect(canvas.getByText('Activity')).toBeTruthy()
    expect(canvas.getByRole('list', { name: 'Messages per day, last 52 weeks' })).toBeTruthy()
    expect(canvas.getByText('Projects')).toBeTruthy()
    expect(canvas.getByText('Memory pipeline')).toBeTruthy()
    expect(canvas.getByText('Models used')).toBeTruthy()
    expect(canvas.getByText('68%')).toBeTruthy()
  },
}

export const EmptyData: Story = {
  args: { stats: null, rangeDays: 7 },
  play: async ({ canvas }) => {
    expect(canvas.getByText('No activity recorded yet.')).toBeTruthy()
    expect(canvas.getByText('No project activity in this period.')).toBeTruthy()
    expect(canvas.getByText('No model turns in this period.')).toBeTruthy()
    expect(canvas.getAllByText('Messages sent, last 7 days.')).toHaveLength(2)
    const hours = canvas.getByRole('img')
    expect(hours.getAttribute('aria-label')).toBe('Messages by hour of day, none in this period')
  },
}

export const HeatmapFocus: Story = {
  play: async ({ canvas }) => {
    const list = canvas.getByRole('list', { name: 'Messages per day, last 52 weeks' })
    const scroll = list.parentElement as HTMLElement
    scroll.focus()
    expect(document.activeElement).toBe(scroll)
    expect(scroll.getAttribute('tabindex')).toBe('0')
  },
}