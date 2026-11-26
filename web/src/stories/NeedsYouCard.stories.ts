import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import NeedsYouCard from '../components/NeedsYouCard.vue'
import { CHAT_ID, chatAction, pendingWrite, refusedWrite, renderBody } from './needsYouFixture'

const props = (state: Parameters<typeof chatAction>[0]) => ({ chatId: CHAT_ID, action: chatAction(state, 'permission_mode', { mode: 'plan' }) })
const meta = { id: 'needs-you-card', title: 'Chat activity/NeedsYouCard', component: NeedsYouCard, args: props('open'), tags: ['!autodocs'], parameters: { docs: { disable: true, story: { inline: false } }, controls: { disable: true } } } satisfies Meta<typeof NeedsYouCard>
export default meta
type Story = StoryObj<typeof meta>
export const Open: Story = { render: () => renderBody(NeedsYouCard, props('open')), play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Not now' })).toBeEnabled() } }
export const Working: Story = { render: () => renderBody(NeedsYouCard, props('working')), play: async ({ canvas }) => { expect(await canvas.findByRole('group')).toHaveAttribute('aria-busy', 'true') } }
export const Done: Story = { render: () => renderBody(NeedsYouCard, props('done')), play: async ({ canvas }) => { expect(await canvas.findByRole('button', { name: 'Done' })).toBeTruthy() } }
export const PendingDismissal: Story = {
  parameters: { fixture: { routes: [pendingWrite('dismiss')] } }, render: () => renderBody(NeedsYouCard, props('open')),
  play: async ({ canvas, userEvent }) => { const button = await canvas.findByRole('button', { name: 'Not now' }); await userEvent.click(button); await waitFor(() => expect(button).toBeDisabled()) },
}
export const FailedDismissal: Story = {
  parameters: { fixture: { routes: [refusedWrite('dismiss', 'synthetic_failure')] } }, render: () => renderBody(NeedsYouCard, props('open')),
  play: async ({ canvas, userEvent }) => { await userEvent.click(await canvas.findByRole('button', { name: 'Not now' })); expect(await canvas.findByRole('alert')).toHaveTextContent('This card could not be dismissed. Try again.') },
}
