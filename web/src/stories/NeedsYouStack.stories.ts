import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import NeedsYouStack from '../components/NeedsYouStack.vue'
import { CHAT_ID, chatAction, renderBody } from './needsYouFixture'

const open = chatAction('open', 'permission_mode', { mode: 'plan' })
const meta = { id: 'needs-you-stack', title: 'Chat activity/NeedsYouStack', component: NeedsYouStack, args: { chatId: CHAT_ID, actions: [open] }, tags: ['!autodocs'], parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } } } satisfies Meta<typeof NeedsYouStack>
export default meta
type Story = StoryObj<typeof meta>
export const Empty: Story = {
  render: () => renderBody(NeedsYouStack, { chatId: CHAT_ID, actions: [] }),
  play: async ({ canvasElement }) => { await waitFor(() => expect(canvasElement.querySelector('[data-v-app]')).not.toBeNull()); expect(canvasElement.querySelector('.needs-you-stack')).toBeNull() },
}
export const Multiple: Story = {
  render: () => renderBody(NeedsYouStack, { chatId: CHAT_ID, actions: [open, { ...chatAction('working', 'permission_mode', { mode: 'bypass' }), id: 'storybook-action-2' }] }),
  play: async ({ canvas }) => { await waitFor(() => expect(canvas.getAllByRole('group')).toHaveLength(2)); expect(canvas.getByText(/Every command and file edit/)).toBeTruthy() },
}
