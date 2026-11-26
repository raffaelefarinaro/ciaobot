import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import ChatHeaderMoreMenu from '../components/ChatHeaderMoreMenu.vue'
import { assertQuietFence, connectedOwnerStory, seedInbox } from './notificationInboxFixture'
import { headerHistory } from './headerHistoryFixture'

const meta = { id: 'chat-header-more-menu', title: 'Chat/ChatHeaderMoreMenu', component: ChatHeaderMoreMenu, args: { canArchive: true } } satisfies Meta<typeof ChatHeaderMoreMenu>
export default meta
type Story = StoryObj<typeof meta>
const renderMenu = (archive: boolean, count: boolean) => connectedOwnerStory({ component: ChatHeaderMoreMenu, props: { canArchive: archive }, seed: pinia => seedInbox(pinia, count ? { chats: 'rows' } : {}), label: 'chat menu' })
export const Default: Story = {
  render: () => renderMenu(true, false),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More chat actions' }))
    await waitFor(() => expect(screen.getByText('Chat details')).toBeTruthy())
    expect(screen.getByText('Archive')).toBeTruthy()
    expect(screen.getByText('Notifications')).toBeTruthy()
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Chat details')).toBeNull())
  },
}
export const NoArchive: Story = {
  args: { canArchive: false }, render: () => renderMenu(false, false),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More chat actions' }))
    await waitFor(() => expect(screen.getByText('Chat details')).toBeTruthy())
    expect(screen.queryByText('Archive')).toBeNull()
  },
}
export const Unread: Story = {
  render: () => renderMenu(true, true),
  play: async ({ canvas }) => { await assertQuietFence(canvas); expect(canvas.getByRole('button', { name: 'More chat actions, 3 unread notifications' })).toBeTruthy() },
}
const actionOwner = defineComponent({
  setup() {
    const event = ref('none')
    return () => [h(ChatHeaderMoreMenu, { canArchive: true, onDetails: () => { event.value = 'details' }, onArchive: () => { event.value = 'archive' } }), h('p', { 'data-testid': 'menu-event' }, event.value)]
  },
})
export const Actions: Story = {
  render: () => connectedOwnerStory({ component: actionOwner, seed: pinia => seedInbox(pinia), label: 'chat menu' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'More chat actions' }))
    await waitFor(() => expect(screen.getByText('Chat details')).toBeTruthy())
    await userEvent.click(screen.getByText('Chat details'))
    expect(canvas.getByTestId('menu-event').textContent).toBe('details')
    await userEvent.click(canvas.getByRole('button', { name: 'More chat actions' }))
    await waitFor(() => expect(screen.getByText('Archive')).toBeTruthy())
    await userEvent.click(screen.getByText('Archive'))
    expect(canvas.getByTestId('menu-event').textContent).toBe('archive')
  },
}
export const Notifications: Story = {
  render: () => renderMenu(true, true),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const trigger = canvas.getByRole('button', { name: 'More chat actions, 3 unread notifications' })
    await userEvent.click(trigger)
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Notifications' })).toBeTruthy())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Notifications' }))
    const dialog = await waitFor(() => screen.getByRole('dialog', { name: 'Notifications' }))
    await waitFor(() => expect(document.activeElement).toBe(dialog.querySelector('h2')))
    expect(dialog.classList.contains('notif-popover--sheet')).toBe(true)
    await userEvent.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Notifications' })).toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(trigger))
  },
}
export const History: Story = {
  render: () => headerHistory(defineComponent({ render: () => h(ChatHeaderMoreMenu, { canArchive: true }) })),
  play: async ({ canvas, userEvent }) => {
    await waitFor(() => expect(canvas.getByTestId('history-ready').textContent).toBe('ready'))
    await userEvent.click(canvas.getByRole('button', { name: 'More chat actions' }))
    await waitFor(() => expect(screen.getByRole('menuitem', { name: 'Back to Home' })).toBeTruthy())
    await userEvent.click(screen.getByRole('menuitem', { name: 'Back to Home' }))
    await waitFor(() => expect(canvas.getByTestId('history-route').textContent).toBe('/'))
  },
}
