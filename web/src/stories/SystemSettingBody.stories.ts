import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SystemSettingBody from '../components/needs-you/SystemSettingBody.vue'
import { CHAT_ID, chatAction, pendingWrite, refusedWrite, renderBody, settingsDestinations } from './needsYouFixture'

const props = (state: Parameters<typeof chatAction>[0], setting = 'camera') => ({
  chatId: CHAT_ID, action: chatAction(state, 'system_setting', { setting }),
})
const meta = {
  id: 'system-setting-body', title: 'Needs-you stable bodies/SystemSettingBody', component: SystemSettingBody,
  args: props('open'), tags: ['!autodocs'],
  parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } },
} satisfies Meta<typeof SystemSettingBody>
export default meta
type Story = StoryObj<typeof meta>

export const Open: Story = {
  render: () => renderBody(SystemSettingBody, props('open')),
  play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Open System Settings' })).toBeEnabled() },
}
export const Pending: Story = {
  parameters: { fixture: { routes: [pendingWrite('start')] } },
  render: () => renderBody(SystemSettingBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open System Settings' }))
    await waitFor(() => expect(canvas.getByRole('button', { name: 'Opening…' })).toBeDisabled())
  },
}
export const Failed: Story = {
  parameters: { fixture: { routes: [refusedWrite('start', 'synthetic_failure')] } },
  render: () => renderBody(SystemSettingBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open System Settings' }))
    expect(await canvas.findByRole('alert')).toHaveTextContent('System Settings could not be opened. Try again.')
  },
}
export const NotLocal: Story = {
  parameters: { fixture: { routes: [refusedWrite('start', 'not_local')] } },
  render: () => renderBody(SystemSettingBody, props('open')),
  play: async ({ canvas, userEvent }) => {
    await userEvent.click(await canvas.findByRole('button', { name: 'Open System Settings' }))
    expect(await canvas.findByRole('status')).toHaveTextContent('Open this on your Mac: System Settings → Privacy & Security.')
  },
}
export const Done: Story = {
  render: () => renderBody(SystemSettingBody, props('done', 'full_disk_access'), { routerRoutes: settingsDestinations }),
  play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Restart Ciaobot in Settings' })).toBeEnabled() },
}
export const Waiting: Story = {
  render: () => renderBody(SystemSettingBody, props('waiting')),
  play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('.needs-you-system-setting')).not.toBeNull()); expect(canvasElement.querySelector('button')).toBeNull() },
}
