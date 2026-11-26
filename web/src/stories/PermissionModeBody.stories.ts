import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import PermissionModeBody from '../components/needs-you/PermissionModeBody.vue'
import { CHAT_ID, chatAction, pendingWrite, refusedWrite, renderBody } from './needsYouFixture'

const props = (state: Parameters<typeof chatAction>[0], mode = 'plan') => ({
  chatId: CHAT_ID,
  action: chatAction(state, 'permission_mode', { mode, note: 'Inspect the synthetic workspace.' }),
})
const meta = {
  id: 'permission-mode-body', title: 'Needs-you stable bodies/PermissionModeBody', component: PermissionModeBody,
  args: props('open'), tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof PermissionModeBody>
export default meta
type Story = StoryObj<typeof meta>

export const Open: Story = {
  render: () => renderBody(PermissionModeBody, props('open')),
  play: async ({ canvas }) => {
    expect(await canvas.findByText(/Requested/)).toBeTruthy()
    expect(canvas.getByText(/Agent.s reason/)).toBeTruthy()
    expect(canvas.getByRole('button', { name: 'Switch to Plan' })).toBeEnabled()
    expect(canvas.getByText('Now').parentElement?.textContent).toContain('unknown')
  },
}
export const Bypass: Story = {
  render: () => renderBody(PermissionModeBody, props('open', 'bypass')),
  play: async ({ canvas }) => { expect(await canvas.findByText(/Every command and file edit runs without asking/)).toBeTruthy() },
}
export const Done: Story = {
  render: () => renderBody(PermissionModeBody, props('done')),
  play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('.needs-you-perm')).not.toBeNull()); expect(canvasElement.querySelector('button')).toBeNull() },
}
export const Pending: Story = {
  parameters: { fixture: { routes: [pendingWrite('start')] } },
  render: () => renderBody(PermissionModeBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    const button = await canvas.findByRole('button', { name: 'Switch to Plan' })
    await userEvent.click(button)
    await waitFor(() => expect(button).toBeDisabled())
  },
}
export const Failed: Story = {
  parameters: { fixture: { routes: [refusedWrite('start', 'synthetic_failure')] } },
  render: () => renderBody(PermissionModeBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Switch to Plan' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('The mode could not be changed. Try again.')
  },
}
