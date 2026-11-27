import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import ChatSignals from '../components/ChatSignals.vue'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'

const CHAT = 'storybook-chat'
function signal(state: 'empty' | 'needs' | 'working' | 'retry' | 'precedence' | 'memory' | 'schedule' | 'paused' | 'shell' | 'shells' | 'shellsWorking', density: 'row' | 'card' = 'row') {
  return defineComponent({ setup() {
    const pinia = createPinia(); setActivePinia(pinia)
    const projects = useProjectStore(pinia)
    projects.chats = [{ chat_id: CHAT, project_id: 'synthetic', title: 'Synthetic chat', archived: false, local: true,
      pending_question: state === 'needs' || state === 'precedence' ? JSON.stringify({ questions: [{ question: 'Which option?' }] }) : '',
      last_activity_at: state === 'precedence' ? '2026-08-11T12:00:00Z' : '', last_read_at: '2026-08-10T12:00:00Z',
      helper: state === 'memory' || state === 'precedence' ? { kind: 'memory_pass', state: 'attention', source_chat_id: 'source', archive_path: 'chats/source.md', doc_path: 'projects/general.md', source_title: 'Synthetic', source_project: 'General', archive_policy: 'when_clean' } : undefined,
      retry: state === 'retry' ? { status: 'pending' } : undefined, last_error: state === 'retry' ? 'Synthetic error' : '' }] as typeof projects.chats
    projects.projectStreaming = state === 'working' || state === 'precedence' || state === 'memory' || state === 'shellsWorking' ? { [CHAT]: true } : {}
    projects.runningShells = state === 'shell' ? { [CHAT]: 1 } : state === 'shells' || state === 'shellsWorking' ? { [CHAT]: 3 } : {}
    const tasks = useTaskStore(pinia)
    tasks.schedules = (state === 'schedule' || state === 'paused' ? [{ schedule_id: 'synthetic', frequency: 'interval', interval_minutes: 10, web_chat_id: CHAT, enabled: state === 'schedule' }] : []) as typeof tasks.schedules
    return () => h(ChatSignals, { chatId: CHAT, density })
  } })
}
const meta = { id: 'chat-signals', title: 'Chat activity/ChatSignals', component: ChatSignals, args: { chatId: CHAT }, tags: ['!autodocs'], parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } } } satisfies Meta<typeof ChatSignals>
export default meta
type Story = StoryObj<typeof meta>
export const Empty: Story = { render: () => signal('empty'), play: async ({ canvasElement }) => { expect(canvasElement.querySelectorAll('.chat-signal')).toHaveLength(0) } }
export const NeedsYou: Story = { render: () => signal('needs', 'card'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Needs your answer')).toHaveTextContent('needs you') } }
export const Working: Story = { render: () => signal('working'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Working')).toBeTruthy() } }
export const FailedRetry: Story = { render: () => signal('retry'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Run failed, retry scheduled')).toBeTruthy() } }
export const Precedence: Story = { render: () => signal('precedence'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Needs your answer')).toBeTruthy(); expect(canvas.queryByLabelText('Working')).toBeNull(); expect(canvas.queryByLabelText('Memory pass needs you')).toBeNull(); expect(canvas.queryByLabelText('Unread chat')).toBeNull() } }
export const MemoryAttention: Story = { render: () => signal('memory'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Memory pass needs you')).toBeTruthy(); expect(canvas.queryByLabelText('Working')).toBeNull() } }
export const ScheduleRunning: Story = { render: () => signal('schedule'), play: async ({ canvas }) => { expect(canvas.getByLabelText('An automation running in this chat')).toBeTruthy() } }
export const SchedulePaused: Story = { render: () => signal('paused'), play: async ({ canvas }) => { expect(canvas.getByLabelText('An automation attached to this chat (paused)')).toHaveClass('stopped') } }
export const ShellRunning: Story = { render: () => signal('shell'), play: async ({ canvas }) => { expect(canvas.getByLabelText('1 shell running')).toBeTruthy() } }
export const ShellsRunning: Story = { render: () => signal('shells'), play: async ({ canvas }) => { expect(canvas.getByLabelText('3 shells running')).toBeTruthy() } }
export const ShellsWhileWorking: Story = { render: () => signal('shellsWorking'), play: async ({ canvas }) => { expect(canvas.getByLabelText('Working')).toBeTruthy(); expect(canvas.queryByLabelText('3 shells running')).toBeNull() } }
