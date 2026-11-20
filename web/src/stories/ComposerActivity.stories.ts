import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ComposerActivity from '../components/ComposerActivity.vue'
import type { RunningWorkRow } from '../lib/runningWork'

/**
 * The chips beside Send: how many agents and background shells the chat runs right now.
 * The rows are the running cards the store reads (`lib/runningWork.ts`). The chips show
 * only when something runs; the popover lists the rows and is opened by the chip.
 */

const NOW = Date.parse('2026-11-20T10:10:00Z')
const agent = (n: number): RunningWorkRow => ({
  tool_use_id: `toolu_agent_${n}`,
  label: `Explore area ${n}`,
  detail: 'Explore',
  started_at: NOW - n * 42_000,
})
const shell = (n: number): RunningWorkRow => ({
  tool_use_id: `toolu_shell_${n}`,
  label: n === 1 ? 'npm run dev' : `ping -c ${n} 127.0.0.1`,
  detail: '',
  started_at: NOW - n * 90_000,
})

const meta = {
  id: 'composer-activity',
  title: 'Chat activity/ComposerActivity',
  component: ComposerActivity,
  args: { chatId: 'chat-story', agents: [], shells: [], now: NOW },
  render: (args) => ({
    components: { ComposerActivity },
    setup: () => ({ args }),
    template: '<div style="display: flex; align-items: center; justify-content: flex-end; max-width: 560px; padding: var(--space-3); color: var(--fg); font-size: var(--text-sm)"><ComposerActivity v-bind="args" /></div>',
  }),
} satisfies Meta<typeof ComposerActivity>
export default meta
type Story = StoryObj<typeof meta>

// One subagent running: the working glyph and the singular label.
export const OneAgent: Story = {
  args: { agents: [agent(1)] },
  play: async ({ canvasElement }) => {
    const chip = canvasElement.querySelector('.composer-activity-seg--agents')
    expect(chip?.textContent?.trim()).toBe('1 agent')
    expect(canvasElement.querySelector('.composer-activity-seg--runs')).toBeNull()
  },
}

// Only background shells: the run glyph and the plural label, no agent chip.
export const ShellsOnly: Story = {
  args: { shells: [shell(1), shell(2)] },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.composer-activity-seg--agents')).toBeNull()
    expect(canvasElement.querySelector('.composer-activity-seg--runs')?.textContent?.trim()).toBe('2 shells')
  },
}

// Both at once: agents first, then shells, each its own chip.
export const Both: Story = {
  args: { agents: [agent(1), agent(2)], shells: [shell(1)] },
  play: async ({ canvasElement }) => {
    const labels = Array.from(canvasElement.querySelectorAll('.composer-activity-seg')).map(el => el.textContent?.trim())
    expect(labels).toEqual(['2 agents', '1 shell'])
  },
}

// Many: counts past the singular, no truncation of the number.
export const Many: Story = {
  args: { agents: [agent(1), agent(2), agent(3), agent(4), agent(5)], shells: [shell(1), shell(2), shell(3)] },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.composer-activity-seg--agents')?.textContent?.trim()).toBe('5 agents')
    expect(canvasElement.querySelector('.composer-activity-seg--runs')?.textContent?.trim()).toBe('3 shells')
  },
}

// A narrow composer bar folds each chip to its glyph and count.
export const Compact: Story = {
  args: { agents: [agent(1), agent(2)], shells: [shell(1)] },
  render: (args) => ({
    components: { ComposerActivity },
    setup: () => ({ args }),
    template: '<div style="display: flex; align-items: center; justify-content: flex-end; width: 300px; padding: var(--space-3); color: var(--fg); font-size: var(--text-sm)"><ComposerActivity v-bind="args" /></div>',
  }),
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.composer-activity--compact')).not.toBeNull()
    expect(canvasElement.querySelector('.composer-activity-seg--agents .composer-activity-count')?.textContent).toBe('2')
    expect(canvasElement.querySelector('.composer-activity-label')).toBeNull()
  },
}

// Nothing runs: no chip at all, so the composer bar keeps its usual look.
export const None: Story = {
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelectorAll('.composer-activity-seg')).toHaveLength(0)
  },
}
