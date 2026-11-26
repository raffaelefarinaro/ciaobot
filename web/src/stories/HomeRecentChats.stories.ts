import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import HomeRecentChats from '../components/HomeRecentChats.vue'
import type { ChatInfo } from '../lib/types'
import { useProjectStore } from '../stores/projects'
import { intakeProjectId, renderSeeded } from './homeIntakeFixture'
import { json } from './homeWindowsFixture'

const chat: ChatInfo = { chat_id: 'synthetic-chat', project_id: intakeProjectId, title: 'Synthetic conversation', model: 'synthetic-model', provider: 'claude', mode: '', session_id: '', created_at: '2026-01-01T00:00:00Z', last_activity_at: '2026-01-01T00:00:00Z', archived: false }
const meta = { id: 'connected-home-recent-chats', title: 'Connected/Home recent chats', component: HomeRecentChats, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true }, fixture: { routes: [{ method: 'GET', path: '/api/tasks?workspace=storybook-alpha', respond: () => json({ tasks: [] }) }] } } } satisfies Meta<typeof HomeRecentChats>
export default meta
type Story = StoryObj<typeof meta>
export const Loading: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'Initial loading', projects: false }), play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('.home-recent--loading')).toBeTruthy()) } }
export const Empty: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'No activity', seed: pinia => { useProjectStore(pinia).bootstrapped = true } }), play: async ({ canvasElement }) => { expect(canvasElement.querySelector('.home-recent')).toBeNull() } }
export const Active: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'Recent activity', seed: pinia => { const store = useProjectStore(pinia); store.bootstrapped = true; store.chats = [chat] } }), play: async ({ canvas }) => { expect(await canvas.findByText('Synthetic conversation')).toBeTruthy() } }
export const PendingTitle: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'Pending title', seed: pinia => { const store = useProjectStore(pinia); store.bootstrapped = true; store.chats = [{ ...chat, title_status: 'pending' }] } }), play: async ({ canvas }) => { expect(await canvas.findByText('Naming conversation')).toBeTruthy() } }
export const Attention: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'Attention and unread tiers', seed: pinia => {
  const store = useProjectStore(pinia)
  store.bootstrapped = true
  store.chats = [{ ...chat, title: 'Needs attention', last_error: 'Synthetic failure' }, { ...chat, chat_id: 'unread-chat', title: 'Unread answer' }]
} }), play: async ({ canvas }) => {
  expect(await canvas.findByText('needs you')).toBeTruthy()
  expect(canvas.getByText('unread')).toBeTruthy()
  expect(canvas.getByText('Needs attention')).toBeTruthy()
  expect(canvas.getByText('Unread answer')).toBeTruthy()
} }
export const InsightOnly: Story = { render: () => renderSeeded(HomeRecentChats, { label: 'Memory insight without active chats', seed: pinia => {
  const store = useProjectStore(pinia)
  store.bootstrapped = true
  store.chats = [{ ...chat, chat_id: 'source', title: 'Archived conversation', archived: true, archive_path: 'archive/synthetic.md' }, { ...chat, chat_id: 'pass', title: 'Memory pass', helper: { kind: 'memory_pass', state: 'running', source_chat_id: 'source', source_title: 'Archived conversation', archive_path: 'archive/synthetic.md', doc_path: '', source_project: '', archive_policy: 'when_clean' } }]
} }), play: async ({ canvas }) => {
  expect(await canvas.findByText('Archived conversation')).toBeTruthy()
  expect(canvas.getByText('memory insights')).toBeTruthy()
  expect(canvas.queryByText('Memory pass')).toBeNull()
} }
