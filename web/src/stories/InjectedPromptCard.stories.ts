import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import InjectedPromptCard from '../components/InjectedPromptCard.vue'
import type { InjectedPrompt } from '../lib/injectedPrompt'
import { renderBody } from './needsYouFixture'

const failed: InjectedPrompt = { kind: 'background_run', count: 1, runs: [{ label: 'Synthetic check', runId: 'bg-1', verdict: 'failed', exitCode: 3, error: 'exit 3', logPath: '/synthetic/log', tail: ['Check failed'] }] }
const meta = {
  id: 'injected-prompt-card', title: 'Chat activity/InjectedPromptCard', component: InjectedPromptCard,
  args: { prompt: failed, raw: 'Synthetic request' },
} satisfies Meta<typeof InjectedPromptCard>
export default meta
type Story = StoryObj<typeof meta>

export const Failed: Story = {
  play: async ({ canvas, userEvent }) => {
    expect(canvas.getByText('Check failed')).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getAllByText('failed')).toHaveLength(2)
    expect(canvas.getByRole('region', { name: 'Last 1 lines of Synthetic check' })).toBeTruthy()
  },
}
export const NoOutput: Story = {
  args: { prompt: { ...failed, runs: [{ ...failed.runs[0], tail: [], error: '' }] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getByText('No output.')).toBeTruthy()
  },
}
export const Compaction: Story = {
  args: { prompt: { kind: 'compaction', text: '**Synthetic summary** of the conversation' }, raw: '' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getByRole('region', { name: 'Conversation summary' })).toHaveTextContent('Synthetic summary')
  },
}
export const Retry: Story = {
  args: { prompt: { kind: 'retry' }, raw: 'Original synthetic request' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getByText('This turn repeats the request above unchanged.')).toBeTruthy()
  },
}
export const Schedule: Story = {
  args: { prompt: { kind: 'schedule', prompt: 'Summarise the synthetic run.', command: { command: 'check', exitCode: null, timedOut: true, output: 'Synthetic timeout' } }, raw: 'Synthetic request' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    await userEvent.click(canvas.getByRole('button', { name: /Command output/ }))
    expect(canvas.getByRole('region', { name: 'Command output' })).toHaveTextContent('Synthetic timeout')
    expect(canvas.getByText('timed out')).toBeTruthy()
  },
}
export const Webhook: Story = {
  args: { prompt: { kind: 'webhook', instructions: 'Triage the synthetic event.', event: 'Build failed' }, raw: 'Synthetic request', chatTitle: 'Synthetic build' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    await userEvent.click(canvas.getByRole('button', { name: 'Event' }))
    expect(canvas.getByRole('region', { name: 'Webhook event' })).toHaveTextContent('Build failed')
  },
}
export const RestartResume: Story = {
  args: { prompt: { kind: 'restart_resume' }, raw: '' },
  play: async ({ canvas, userEvent }) => { await userEvent.click(canvas.getByRole('button', { expanded: false })); expect(canvas.getByText(/continue where it left off/)).toBeTruthy() },
}
export const ErrorNotice: Story = {
  args: { prompt: { kind: 'error', label: 'Timed out', copy: 'The operation took too long.', text: 'Synthetic timeout', retried: false }, raw: '' },
  play: async ({ canvas }) => { expect(canvas.getByRole('status')).toBeTruthy() },
}
export const CliTaskWake: Story = {
  args: { prompt: { kind: 'cli_task_wake', count: 1, tasks: [{ type: 'build', description: 'Check the synthetic build', taskId: 'task-1', command: 'make check' }] }, raw: 'Synthetic task wake' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getByText(/Check the synthetic build/)).toBeTruthy()
    expect(canvas.getByText('make check')).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'Show full prompt' }))
    expect(canvas.getByRole('region', { name: 'Full prompt' })).toHaveTextContent('Synthetic task wake')
  },
}
export const Loop: Story = {
  args: { prompt: { kind: 'loop', run: 3, of: 12, ends: '14:30 UTC', prompt: 'Check the synthetic build.' }, raw: 'Synthetic loop wake' },
  render: (args) => renderBody(InjectedPromptCard, args),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { expanded: false }))
    expect(canvas.getByRole('region', { name: 'Loop prompt' })).toHaveTextContent('Check the synthetic build.')
    expect(canvas.getByRole('link', { name: 'Open Automations' })).toHaveAttribute('href', '/schedules')
  },
}
export const TaskResume: Story = {
  args: { prompt: { kind: 'task_resume', title: 'Synthetic task', previousState: 'failed' }, raw: 'Continue the synthetic task.' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    expect(canvas.getByText('The previous attempt ended as failed.')).toBeTruthy()
    expect(canvas.getByRole('region', { name: 'Injected request' })).toHaveTextContent('Continue the synthetic task.')
  },
}
export const RetryScheduled: Story = {
  args: { prompt: { kind: 'retry_scheduled' }, raw: '' },
  play: async ({ canvas, userEvent }) => { await userEvent.click(canvas.getByRole('button', { expanded: false })); expect(canvas.getByText('A retry is scheduled.')).toBeTruthy() },
}
export const LoopStop: Story = {
  args: { prompt: { kind: 'loop_stop', text: 'Loop stopped after two failed runs.' }, raw: '' },
  play: async ({ canvas, userEvent }) => { await userEvent.click(canvas.getByRole('button', { expanded: false })); expect(canvas.getByText('Loop stopped after two failed runs.')).toBeTruthy() },
}
export const Handover: Story = {
  args: { prompt: { kind: 'handover', from: 'Agent A', to: 'Agent B', sent: 'Synthetic handover context' }, raw: '' },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(canvas.getByRole('button', { expanded: false }))
    await userEvent.click(canvas.getByText('Show what the agent received'))
    expect(canvas.getByRole('region', { name: 'What the agent received' })).toHaveTextContent('Synthetic handover context')
  },
}
export const HandoverEmpty: Story = {
  args: { prompt: { kind: 'handover', from: 'Agent A', to: 'Agent B', sent: '' }, raw: '' },
  play: async ({ canvas, userEvent }) => { await userEvent.click(canvas.getByRole('button', { expanded: false })); expect(canvas.getByText('This handover recorded no copy of what was sent.')).toBeTruthy(); expect(canvas.queryByText('Show what the agent received')).toBeNull() },
}
