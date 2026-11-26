import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount } from 'vue'
import ProviderMcpButton from '../components/ProviderMcpButton.vue'
import type { ProviderConnection } from '../lib/types'
import { ownerPanel } from './settingsAccessFixture'

const connection: ProviderConnection = { name: 'claude', label: 'Claude', ok: true, auth: 'ready', state: 'ready', command: 'claude', mcp_command: 'claude', mcp_slash: '/mcp' }
const meta = {
  id: 'provider-mcp-button', title: 'Settings/ProviderMcpButton', component: ProviderMcpButton,
  args: { connection }, tags: ['!autodocs'], parameters: { docs: { disable: true }, controls: { disable: true } },
} satisfies Meta<typeof ProviderMcpButton>
export default meta
type Story = StoryObj<typeof meta>
function panel(row: ProviderConnection) {
  return ownerPanel({ label: 'ProviderMcpButton', component: defineComponent({ setup: () => () => h(ProviderMcpButton, { connection: row }) }) })
}
export const Available: Story = {
  render: () => panel(connection),
  play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Open MCP settings in Claude' })).toBeEnabled() },
}
export const Uninstalled: Story = {
  render: () => panel({ ...connection, mcp_command: undefined }),
  play: async ({ canvas }) => {
    await canvas.findByText(/Fixture requests: 0/)
    expect(canvas.queryByRole('button')).toBeNull()
  },
}

export const Copied: Story = {
  render: () => ownerPanel({ label: 'ProviderMcpButton', clipboard: true, component: defineComponent({ setup: () => () => h(ProviderMcpButton, { connection }) }) }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open MCP settings in Claude' }))
    expect(await canvas.findByText(/command was copied/)).toBeTruthy()
  },
}
export const CopyFailed: Story = {
  render: () => ownerPanel({ label: 'ProviderMcpButton', component: defineComponent({ setup() {
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const exec = document.execCommand
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('Synthetic refusal') } } })
    document.execCommand = () => false
    onBeforeUnmount(() => {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
      document.execCommand = exec
    })
    return () => h(ProviderMcpButton, { connection })
  } }) }),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open MCP settings in Claude' }))
    expect(await canvas.findByText(/Could not reach the terminal or the clipboard/)).toBeTruthy()
  },
}
