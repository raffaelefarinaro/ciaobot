import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { h } from 'vue'
import { signInPanel } from './signInFixture'
import { resetHostTerminalForTests } from '../composables/useHostTerminal'
import ProviderSignInHint from '../components/ProviderSignInHint.vue'
import { held, json, route } from './onboardingStagesFixture'
const meta = { id: 'provider-sign-in-hint', title: 'Onboarding runtime/Sign in hint', component: ProviderSignInHint, args: { provider: 'claude', actionLabel: 'Retry' }, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } , render: (args) => h(signInPanel(ProviderSignInHint, { ...args })) } satisfies Meta<typeof ProviderSignInHint>
export default meta
type Story = StoryObj<typeof meta>
const open = async (canvasElement: HTMLElement, userEvent: any) => { await userEvent.click(await waitFor(() => { const el = canvasElement.querySelector('summary'); expect(el).toBeTruthy(); return el! })) }
export const Closed: Story = { play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('details')?.open).toBe(false)) } }
export const Loading: Story = { parameters: { fixture: { routes: [route('GET', '/api/settings/providers', held)] } }, play: async ({ canvasElement, userEvent }) => { await open(canvasElement, userEvent); await waitFor(() => expect(canvasElement.textContent).toContain('Looking up the sign-in command')) } }
export const Command: Story = { parameters: { fixture: { routes: [route('GET', '/api/settings/providers', () => json({ connections: { claude: { command: 'storybook login', label: 'Storybook' } } }))] } }, play: async ({ canvasElement, userEvent }) => { await open(canvasElement, userEvent); await waitFor(() => expect(canvasElement.textContent).toContain('storybook login')); expect(canvasElement.textContent).toContain('Retry') } }
export const Missing: Story = { parameters: { fixture: { routes: [route('GET', '/api/settings/providers', () => json({ connections: {} }))] } }, play: async ({ canvasElement, userEvent }) => { await open(canvasElement, userEvent); await waitFor(() => expect(canvasElement.querySelector('[role=alert]')?.textContent).toContain('Could not find')) } }
export const Failure: Story = { parameters: { fixture: { routes: [route('GET', '/api/settings/providers', () => json({}, 503))] } }, play: async ({ canvasElement, userEvent }) => { await open(canvasElement, userEvent); await waitFor(() => expect(canvasElement.querySelector('[role=alert]')?.textContent).toContain('Could not look up')) } }

export const ChatAndHostTerminal: Story = {
  args: { chatId: 'storybook-chat', actionDisabled: true },
  beforeEach: () => { resetHostTerminalForTests(); return () => resetHostTerminalForTests() },
  parameters: { fixture: { routes: [route('GET', '/api/settings/providers', () => json({ connections: { claude: { command: 'storybook login', label: 'Storybook', terminal_action: 'claude-login' } } })), route('GET', '/api/host/terminal', () => json({ available: true, platform: 'storybook', reason: '', terminal: 'storybook', actions: ['claude-login'] }))] } },
  play: async ({ canvasElement, userEvent }) => { await open(canvasElement, userEvent); await waitFor(() => expect(canvasElement.textContent).toContain('Run in this chat\'s terminal')); await waitFor(() => expect([...canvasElement.querySelectorAll('button')].some(button => button.textContent === 'Run in Terminal')).toBe(true)); const retry = [...canvasElement.querySelectorAll('button')].find(button => button.textContent === 'Retry')!; expect(retry.disabled).toBe(true); expect(canvasElement.textContent).toContain('storybook login') },
}
