import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ChatRetryState from '../components/ChatRetryState.vue'
import type { ChatRetryInfo } from '../lib/types'
import { renderBody } from './needsYouFixture'

const retry: ChatRetryInfo = { status: 'pending', next_at: '2099-01-01T12:00:00Z', last_error: '', attempts: 2, interval_seconds: 300, mode: 'continue_recovery' }
const meta = {
  id: 'chat-retry-state', title: 'Chat activity/ChatRetryState', component: ChatRetryState,
  args: { retry, provider: 'claude', streaming: false, recoveryReady: true },
} satisfies Meta<typeof ChatRetryState>
export default meta
type Story = StoryObj<typeof meta>

export const Recovery: Story = {
  play: async ({ canvas }) => {
    expect(canvas.getByText('Retrying this turn shortly')).toBeTruthy()
    expect(canvas.getByText(/2 attempts/)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Continue with...' })).toBeEnabled()
  },
}
export const Unverified: Story = {
  args: { recoveryReady: false, retry: { ...retry, next_at: 'invalid', attempts: 0 } },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('button', { name: 'Continue with...' })).toBeDisabled()
    expect(canvas.getByText(/failure and its history are verified/)).toBeTruthy()
    expect(canvas.queryByText(/Next try/)).toBeNull()
  },
}
export const MissingSchedule: Story = {
  args: { retry: { ...retry, mode: 'literal', kind: 'quota' } },
  play: async ({ canvas }) => {
    expect(canvas.getByText('Retrying this turn')).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Run scheduled prompt' })).toBeDisabled()
    expect(canvas.getByText(/identity can be verified/)).toBeTruthy()
  },
}
export const WithFix: Story = {
  args: { retry: { ...retry, mode: 'literal', schedule_id: 's1' }, canFix: true },
  play: async ({ canvas }) => {
    const labels = canvas.getAllByRole('button').map(b => b.textContent)
    expect(labels).toEqual(['Run scheduled prompt', 'Stop trying', 'Fix this error'])
  },
}
export const Streaming: Story = {
  args: { streaming: true },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('button', { name: 'Try now' })).toBeDisabled()
    expect(canvas.getByRole('button', { name: 'Stop trying' })).toBeEnabled()
  },
}
export const SignedOut: Story = {
  args: { retry: { ...retry, last_error: 'OAuth session expired' } },
  render: (args) => renderBody(ChatRetryState, args),
  parameters: { fixture: { routes: [{ method: 'GET', path: '/api/settings/providers', respond: () => new Response(JSON.stringify({ connections: { claude: { command: 'claude auth login', label: 'Claude' } } }), { headers: { 'Content-Type': 'application/json' } }) }] } },
  play: async ({ canvas, userEvent }) => {
    expect(await canvas.findByText('Signed out — retrying this turn')).toBeTruthy()
    await userEvent.click(await canvas.findByText('How to sign in again'))
    expect(await canvas.findByText('claude auth login')).toBeTruthy()
  },
}
export const SignInLookupFailed: Story = {
  args: { retry: { ...retry, last_error: 'OAuth session expired' } },
  render: (args) => renderBody(ChatRetryState, args),
  parameters: { fixture: { routes: [{ method: 'GET', path: '/api/settings/providers', respond: () => new Response('{}', { status: 500 }) }] } },
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByText('How to sign in again'))
    expect(await canvas.findByRole('alert')).toHaveTextContent('Could not look up the sign-in command')
  },
}
