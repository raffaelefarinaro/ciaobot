import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount } from 'vue'
import HostTerminalButton from '../components/HostTerminalButton.vue'
import { resetHostTerminalForTests } from '../composables/useHostTerminal'
import { held, json, route } from './settingsAccessFixture'

const capabilities = route('GET', '/api/host/terminal', () => json({ available: true, platform: 'darwin', terminal: 'Terminal', actions: ['claude-login'] }))
const meta = {
  id: 'host-terminal-button', title: 'Terminal/HostTerminalButton', component: HostTerminalButton,
  tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta<typeof HostTerminalButton>
export default meta
type Story = StoryObj<typeof meta>
function renderButton() {
  return defineComponent({ setup() {
    resetHostTerminalForTests()
    onBeforeUnmount(resetHostTerminalForTests)
    return () => h(HostTerminalButton, { action: 'claude-login', safeErrors: true })
  } })
}
export const Opened: Story = {
  parameters: { fixture: { routes: [capabilities, route('POST', '/api/host/terminal', () => json({ launched: 'Terminal' }))] } },
  render: renderButton,
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Run in Terminal' }))
    expect(await canvas.findByRole('status')).toHaveTextContent('Opened in Terminal')
  },
}
export const Failure: Story = {
  parameters: { fixture: { routes: [capabilities, route('POST', '/api/host/terminal', () => json({ reason: 'no_terminal_found' }, 500))] } },
  render: renderButton,
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Run in Terminal' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('No supported terminal app was found')
  },
}
export const Opening: Story = {
  parameters: { fixture: { routes: [capabilities, route('POST', '/api/host/terminal', held)] } },
  render: renderButton,
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Run in Terminal' }))
    expect(canvas.getByRole('button', { name: 'Opening…' })).toBeDisabled()
  },
}
