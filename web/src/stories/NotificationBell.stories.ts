import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import NotificationBell from '../components/NotificationBell.vue'
import {
  assertQuietFence,
  connectedOwnerStory,
  seedInbox,
  storyRoute,
} from './notificationInboxFixture'

// The bell is a thin trigger over the notification inbox projection. Rows are
// counted from the real stores; the dialog itself is teleported to the body, so
// the plays assert against the document, not the story canvas.
const meta = {
  id: 'notification-bell',
  title: 'Notices and notifications/NotificationBell',
  component: NotificationBell,
  tags: ['!autodocs'],
} satisfies Meta<typeof NotificationBell>
export default meta
type Story = StoryObj<typeof meta>

function renderBell(seed: Parameters<typeof seedInbox>[1], props: Record<string, unknown> = {}) {
  return connectedOwnerStory({
    component: NotificationBell,
    props,
    seed: (pinia) => seedInbox(pinia, seed),
    label: 'notification bell',
  })
}

/** The real owner behind a host control, for the variant where another control opens the sheet. */
const NoTriggerHost = defineComponent({
  setup() {
    const bell = ref<InstanceType<typeof NotificationBell>>()
    const opener = ref<HTMLButtonElement>()
    return () => h('div', [
      h('button', {
        ref: opener,
        type: 'button',
        onClick: () => bell.value?.open(opener.value),
      }, 'Open notifications'),
      h(NotificationBell, { ref: bell, trigger: false, variant: 'sheet' }),
    ])
  },
})

const dialogIn = (selector: string) => document.body.querySelector(selector) as HTMLElement | null

export const Default: Story = {
  render: () => renderBell({}),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const bell = canvas.getByRole('button', { name: 'Notifications, none' })
    expect(bell.getAttribute('aria-haspopup')).toBe('dialog')
    expect(bell.getAttribute('aria-expanded')).toBe('false')
    expect(bell.getAttribute('title')).toBe('Notifications')
    // Nothing to count and nothing stale: no badge, no ring.
    expect(canvasElement.querySelector('.notif-bell-count')).toBeNull()
    expect(canvasElement.querySelector('.notif-bell-ring')).toBeNull()
  },
}

export const Count: Story = {
  render: () => renderBell({ chats: 'rows' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const bell = canvas.getByRole('button', { name: 'Notifications, 3 items' })
    const badge = canvasElement.querySelector('.notif-bell-count') as HTMLElement
    expect(badge.textContent).toBe('3')
    // The badge is decoration; the button's name carries the count.
    expect(badge.getAttribute('aria-hidden')).toBe('true')
    expect(canvasElement.querySelector('.notif-bell-ring')).toBeNull()
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  },
}

export const CountOverflow: Story = {
  render: () => renderBell({ operator: 'many' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // The count caps the badge but never the accessible name.
    canvas.getByRole('button', { name: 'Notifications, 120 items' })
    expect(canvasElement.querySelector('.notif-bell-count')!.textContent).toBe('99+')
  },
}

export const Partial: Story = {
  render: () => renderBell({
    health: { operator: { status: 'stale', lastSuccessAt: '2026-11-18T12:00:00.000Z', error: 'Synthetic source failure' } },
  }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // A stale source means the count is not the whole story: a hollow ring,
    // and words in the accessible name.
    const bell = canvas.getByRole('button', { name: 'Notifications, none. Some sources could not be fully checked' })
    expect(canvasElement.querySelector('.notif-bell-ring')).not.toBeNull()
    expect(canvasElement.querySelector('.notif-bell-count')).toBeNull()
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  },
}

export const Popover: Story = {
  render: () => renderBell({ chats: 'rows' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const bell = canvas.getByRole('button', { name: 'Notifications, 3 items' })
    await userEvent.click(bell)
    await waitFor(() => expect(dialogIn('.notif-popover')).not.toBeNull())
    const dialog = dialogIn('.notif-popover')!
    expect(dialog.getAttribute('role')).toBe('dialog')
    expect(dialog.getAttribute('aria-label')).toBe('Notifications')
    expect(dialog.classList.contains('notif-popover--popover')).toBe(true)
    expect(bell.getAttribute('aria-expanded')).toBe('true')
    // The count is repeated in the header and the list's own summary renders.
    expect(dialog.querySelector('.notif-popover-count')!.textContent).toBe('3')
    expect(dialog.querySelector('[data-testid="inbox-summary"]')!.textContent).toContain('3 items need a look.')
    // Opening moves focus into the dialog, to its heading.
    await waitFor(() => expect(document.activeElement).toBe(dialog.querySelector('h2')))
    expect(dialog.querySelector('h2')!.getAttribute('tabindex')).toBe('-1')
    await userEvent.click(dialog.querySelector('button[aria-label="Close notifications"]') as HTMLElement)
    await waitFor(() => expect(dialogIn('.notif-popover')).toBeNull())
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  },
}

export const Escape: Story = {
  render: () => renderBell({ chats: 'rows' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const bell = canvas.getByRole('button', { name: 'Notifications, 3 items' })
    await userEvent.click(bell)
    await waitFor(() => expect(dialogIn('.notif-popover')).not.toBeNull())
    await waitFor(() => expect(document.activeElement).toBe(dialogIn('.notif-popover')!.querySelector('h2')))
    await userEvent.keyboard('{Escape}')
    // Escape closes the popover and puts focus back on the bell that opened it.
    await waitFor(() => expect(dialogIn('.notif-popover')).toBeNull())
    expect(document.activeElement).toBe(bell)
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  },
}

export const Sheet: Story = {
  render: () => renderBell({ chats: 'rows' }, { variant: 'sheet' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const bell = canvas.getByRole('button', { name: 'Notifications, 3 items' })
    await userEvent.click(bell)
    await waitFor(() => expect(dialogIn('.notif-popover')).not.toBeNull())
    const dialog = dialogIn('.notif-popover')!
    expect(dialog.classList.contains('notif-popover--sheet')).toBe(true)
    // The sheet owns a backdrop; the popover does not.
    const backdrop = dialogIn('.notif-backdrop')
    expect(backdrop).not.toBeNull()
    await userEvent.click(backdrop!)
    await waitFor(() => expect(dialogIn('.notif-popover')).toBeNull())
    await waitFor(() => expect(dialogIn('.notif-backdrop')).toBeNull())
    expect(document.activeElement).toBe(bell)
  },
}

export const ExternalTrigger: Story = {
  render: () => connectedOwnerStory({
    component: NoTriggerHost,
    seed: (pinia) => seedInbox(pinia, { chats: 'rows' }),
    label: 'notification bell without its own trigger',
  }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    // No bell button: another control (the chat header's menu) owns opening.
    expect(canvas.queryByRole('button', { name: /^Notifications/ })).toBeNull()
    await userEvent.click(canvas.getByRole('button', { name: 'Open notifications' }))
    await waitFor(() => expect(dialogIn('.notif-popover')).not.toBeNull())
    const dialog = dialogIn('.notif-popover')!
    expect(dialog.classList.contains('notif-popover--sheet')).toBe(true)
    expect(dialog.querySelector('[data-testid="inbox-summary"]')!.textContent).toContain('3 items need a look.')
    await waitFor(() => expect(document.activeElement).toBe(dialog.querySelector('h2')))
  },
}

export const Navigate: Story = {
  render: () => renderBell({ operator: 'one' }, { variant: 'sheet' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Notifications, 2 items' }))
    await waitFor(() => expect(dialogIn('.notif-popover')).not.toBeNull())
    const row = dialogIn('.notif-popover')!.querySelector('[data-testid="row-operator:shared:synthetic-check-1"] a') as HTMLElement
    expect(row).not.toBeNull()
    await userEvent.click(row)
    // Choosing a row closes the dialog; the row's own link carries the notice.
    await waitFor(() => expect(dialogIn('.notif-popover')).toBeNull())
    await waitFor(() => expect(storyRoute(canvas)).toContain('notice='))
  },
}
