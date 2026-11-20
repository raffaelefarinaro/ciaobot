import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import SubagentCard from '../components/SubagentCard.vue'
import { subagentCardFromHistory, type SubagentCard as Card, type SubagentStep } from '../lib/subagentCard'

/**
 * One subagent call as a card in the activity trace. The data is the shape the
 * live stream and reloaded history both produce (`lib/subagentCard.ts`), so the
 * stories hand the component the card directly. Plays check the disclosure
 * state, the state word and the nested rows.
 */

const sample = (overrides: Partial<Card> = {}): Card => ({
  tool_use_id: 'sb-sa-1',
  agent_type: 'general',
  description: 'Sleep 5 then reply done',
  state: 'running',
  step_count: 2,
  steps: [
    { kind: 'activity', line: '$ sleep 5', tool_use_id: 'sb-k-1' },
    { kind: 'file', file_path: '/work/app/notes.md', action: 'created', tool: 'Write', tool_use_id: 'sb-k-2' },
  ] satisfies SubagentStep[],
  ...overrides,
})

const meta = {
  id: 'subagent-card',
  title: 'Chat activity/SubagentCard',
  component: SubagentCard,
  args: { card: sample() },
  render: (args) => ({
    components: { SubagentCard },
    setup: () => ({ args }),
    template: '<div style="max-width: 520px; padding: var(--space-3); color: var(--fg); font-size: var(--text-sm)"><SubagentCard v-bind="args" /></div>',
  }),
} satisfies Meta<typeof SubagentCard>
export default meta
type Story = StoryObj<typeof meta>

// A subagent still working: open, with the working glyph and its steps.
export const Running: Story = {
  play: async ({ canvasElement }) => {
    const toggle = canvasElement.querySelector('.subagent-card-toggle')
    expect(toggle?.getAttribute('aria-expanded')).toBe('true')
    expect(canvasElement.querySelector('.subagent-card-state-label')?.textContent).toBe('Running')
    expect(canvasElement.querySelector('.activity-glyph--working')).not.toBeNull()
    expect(canvasElement.querySelectorAll('.subagent-card-body .activity-line')).toHaveLength(1)
    expect(canvasElement.querySelectorAll('.subagent-card-file')).toHaveLength(1)
  },
}

// A finished subagent folds itself; its steps stay one click away.
export const Done: Story = {
  args: { card: sample({ state: 'done' }) },
  play: async ({ canvasElement }) => {
    const toggle = canvasElement.querySelector('.subagent-card-toggle')
    expect(toggle?.getAttribute('aria-expanded')).toBe('false')
    expect(canvasElement.querySelector('.subagent-card-state-label')?.textContent).toBe('Done')
    expect(canvasElement.querySelector('.subagent-card-body')).toBeNull()
  },
}

// A failed subagent: the state is a word and the card carries the failed border.
export const Failed: Story = {
  args: { card: sample({ state: 'failed' }) },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.subagent-card--failed')).not.toBeNull()
    expect(canvasElement.querySelector('.subagent-card-state-label')?.textContent).toBe('Failed')
    expect(canvasElement.querySelector('.subagent-card-toggle')?.getAttribute('aria-expanded')).toBe('false')
  },
}

// Many children: the first fifty rows are drawn and the rest are a count.
export const ManyChildren: Story = {
  args: {
    card: sample({
      state: 'running',
      step_count: 73,
      steps: Array.from({ length: 50 }, (_, n) => ({ kind: 'activity', line: `$ step ${n + 1}`, tool_use_id: `sb-m-${n}` }) as SubagentStep),
    }),
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelectorAll('.subagent-card-body .activity-line')).toHaveLength(50)
    expect(canvasElement.querySelector('.subagent-card-more')?.textContent).toBe('+23 more')
    expect(canvasElement.querySelector('.subagent-card-count')?.textContent).toBe('73 steps')
  },
}

// A journal written before cards existed: the input was the call's title, so
// there is no description, no type and no steps.
export const OldJournal: Story = {
  args: {
    card: subagentCardFromHistory({ tool_use_id: 'sb-old-1', state: 'done' }) as Card,
  },
  play: async ({ canvasElement }) => {
    expect(canvasElement.querySelector('.subagent-card-title')?.textContent).toBe('Subagent')
    expect(canvasElement.querySelector('.subagent-card-type')).toBeNull()
    expect(canvasElement.querySelector('.subagent-card-state-label')?.textContent).toBe('Done')
  },
}
