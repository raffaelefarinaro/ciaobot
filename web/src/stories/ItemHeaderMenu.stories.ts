import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h } from 'vue'
import { DropdownMenuItem } from 'reka-ui'
import ItemHeaderMenu from '../components/ItemHeaderMenu.vue'
import { assertQuietFence, connectedOwnerStory, seedInbox } from './notificationInboxFixture'
import { headerHistory } from './headerHistoryFixture'

const meta = { id: 'item-header-menu', title: 'Chat/ItemHeaderMenu', component: ItemHeaderMenu, args: { label: 'More task actions' } } satisfies Meta<typeof ItemHeaderMenu>
export default meta
type Story = StoryObj<typeof meta>
const owner = defineComponent({ render: () => h(ItemHeaderMenu, { label: 'More task actions' }, { default: () => h(DropdownMenuItem, { asChild: true }, { default: () => h('button', 'Edit task') }) }) })
const phoneOwner = defineComponent({ render: () => h(ItemHeaderMenu, { label: 'More task actions', phoneOnly: true }, { default: () => h(DropdownMenuItem, { asChild: true }, { default: () => h('button', 'Edit task') }) }) })
export const Default: Story = {
  render: () => connectedOwnerStory({ component: owner, seed: pinia => seedInbox(pinia), label: 'item menu' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More task actions' }))
    await waitFor(() => expect(screen.getByText('Edit task')).toBeTruthy())
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Edit task')).toBeNull())
  },
}
export const Phone: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => connectedOwnerStory({ component: owner, seed: pinia => seedInbox(pinia, { chats: 'rows' }), label: 'item menu' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More task actions, 3 unread notifications' }))
    await waitFor(() => expect(screen.getByText('Notifications')).toBeTruthy())
  },
}
export const PhoneOnlyWide: Story = {
  render: () => connectedOwnerStory({ component: phoneOwner, seed: pinia => seedInbox(pinia), label: 'item menu' }),
  play: async ({ canvas }) => { await assertQuietFence(canvas); expect(canvas.queryByRole('button', { name: 'More task actions' })).toBeNull() },
}
export const Notifications: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => connectedOwnerStory({ component: owner, seed: pinia => seedInbox(pinia, { chats: 'rows' }), label: 'item menu' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const trigger = canvas.getByRole('button', { name: 'More task actions, 3 unread notifications' })
    await userEvent.click(trigger)
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Notifications' })).toBeTruthy())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Notifications' }))
    const dialog = await waitFor(() => screen.getByRole('dialog', { name: 'Notifications' }))
    await waitFor(() => expect(document.activeElement).toBe(dialog.querySelector('h2')))
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  },
}
export const History: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => headerHistory(owner),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByTestId('history-ready').textContent).toBe('ready'))
    await userEvent.click(canvas.getByRole('button', { name: 'More task actions' }))
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Back to Home' })).toBeTruthy())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Back to Home' }))
    await waitFor(() => expect(canvas.getByTestId('history-route').textContent).toBe('/'))
  },
}
