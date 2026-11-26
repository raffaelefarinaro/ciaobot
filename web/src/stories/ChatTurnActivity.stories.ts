import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ChatTurnActivity from '../components/ChatTurnActivity.vue'
import type { ChatMessage } from '../lib/types'

const step = (partial: Partial<ChatMessage>) => ({ role: 'assistant', content: '', timestamp: '', ...partial }) as ChatMessage
const meta = {
  id: 'chat-turn-activity', title: 'Chat activity/ChatTurnActivity', component: ChatTurnActivity,
  args: { steps: [step({ content: 'Checking the synthetic config' })], open: false, thinkingExpanded: false, renderMarkdown: (s: string) => s, renderActivityLine: (s: string) => s, outputsId: 'outputs-story' },
} satisfies Meta<typeof ChatTurnActivity>
export default meta
type Story = StoryObj<typeof meta>

export const Collapsed: Story = {
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { expanded: false })).toHaveTextContent('Activity') },
}
export const Expanded: Story = {
  args: { open: true, steps: [step({ tool_name: '_activity', content: 'Read config\n↳ [Explore] searching' }), step({ tool_name: '_filecard', file_path: 'notes/plan.md', action: 'created' })] },
  play: async ({ canvas }) => { expect(canvas.getByText('Read config')).toBeTruthy(); expect(canvas.getByRole('button', { name: /Wrote plan.md/ })).toBeTruthy() },
}
export const Empty: Story = {
  args: { open: true, steps: [] },
  play: async ({ canvasElement }) => { expect(canvasElement.querySelectorAll('.trace-step')).toHaveLength(0) },
}
export const LazyThinking: Story = {
  args: { open: true, thinkingExpanded: true, steps: [step({ tool_name: '_thinking', lazy: true, i: 1 })] },
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: 'Load full reasoning…' })).toBeTruthy() },
}
const outputs = [{ file_path: 'synthetic/report.md', action: 'created' }, { file_path: 'synthetic/notes.md', action: 'edited' }]
export const OutputsCollapsed: Story = {
  args: { open: true, outputs, outputsOpen: false },
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: /Outputs/ })).toHaveAttribute('aria-expanded', 'false'); expect(canvas.queryByText('report.md')).toBeNull() },
}
export const OutputsExpanded: Story = {
  args: { open: true, outputs, outputsOpen: true },
  play: async ({ canvas }) => { expect(canvas.getByRole('button', { name: /Outputs/ })).toHaveAttribute('aria-expanded', 'true'); expect(canvas.getByRole('button', { name: /report.md/ })).toBeTruthy(); expect(canvas.getByText('edited')).toBeTruthy() },
}
