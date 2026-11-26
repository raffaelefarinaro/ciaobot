import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { h } from 'vue'
import { signInPanel } from './signInFixture'
import { useTerminalStore } from '../stores/terminal'
import ProviderSignInGuidance from '../components/settings/ProviderSignInGuidance.vue'
import { WORKSPACE } from './onboardingStagesFixture'
const meta = { id: 'provider-sign-in-guidance', title: 'Onboarding runtime/Sign in guidance', component: ProviderSignInGuidance, args: { workspace: WORKSPACE, context: 'Storybook', command: 'storybook login' }, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } } , render: (args) => h(signInPanel(ProviderSignInGuidance, { ...args })) } satisfies Meta<typeof ProviderSignInGuidance>
export default meta
type Story = StoryObj<typeof meta>
export const Default: Story = { play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.textContent).toContain('storybook login')); expect(canvasElement.textContent).toContain('terminal runs on the engine host') } }
export const CustomNote: Story = { args: { note: 'Synthetic provider guidance.' }, play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic provider guidance.')) } }
export const NoWorkspace: Story = { args: { workspace: '' }, play: async ({ canvasElement, userEvent }) => { await userEvent.click(await waitFor(() => { const button = canvasElement.querySelectorAll('button')[1]; expect(button).toBeTruthy(); return button })); await waitFor(() => expect(canvasElement.querySelector('[role=status]')?.textContent).toContain('Select a workspace')) } }

export const BusyTerminal: Story = {
  render: args => h(signInPanel(ProviderSignInGuidance, { ...args }, pinia => { const terminal = useTerminalStore(pinia); terminal.visible = true; terminal.target = { workspace: 'storybook-beta' } })),
  play: async ({ canvasElement, userEvent }) => { await userEvent.click(await waitFor(() => { const button = canvasElement.querySelectorAll('button')[1]; expect(button).toBeTruthy(); return button })); await waitFor(() => expect(canvasElement.querySelector('[role=status]')?.textContent).toContain('Another terminal is open')) },
}
export const InvalidWorkspace: Story = { args: { workspace: ' invalid ' }, play: async ({ canvasElement, userEvent }) => { await userEvent.click(await waitFor(() => { const button = canvasElement.querySelectorAll('button')[1]; expect(button).toBeTruthy(); return button })); await waitFor(() => expect(canvasElement.querySelector('[role=status]')?.textContent).toContain('workspace is unavailable')) } }
