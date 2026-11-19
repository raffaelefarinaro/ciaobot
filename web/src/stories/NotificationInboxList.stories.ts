import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import NotificationInboxList from '../components/NotificationInboxList.vue'
import {
  assertQuietFence,
  connectedOwnerStory,
  inboxRoutes,
  seedInbox,
  stale,
  unavailable,
  storyRoute,
} from './notificationInboxFixture'
import type { InboxSeed } from './notificationInboxFixture'

// The inbox list under the bell. Every row is projected from the real stores;
// the fixture seeds source health and rows, and only the two interactions that
// re-read a source declare their exact routes.
const meta = {
  id: 'notification-inbox-list',
  title: 'Notices and notifications/NotificationInboxList',
  component: NotificationInboxList,
  tags: ['!autodocs'],
} satisfies Meta<typeof NotificationInboxList>
export default meta
type Story = StoryObj<typeof meta>

function renderList(seed: InboxSeed, props: Record<string, unknown> = {}) {
  return connectedOwnerStory({
    component: NotificationInboxList,
    props,
    seed: (pinia) => seedInbox(pinia, seed),
    label: 'notification inbox list',
  })
}

const headingText = (element: Element) => element.textContent!.replace(/\s+/g, ' ').trim()

export const Checking: Story = {
  render: () => renderList({ updateTasksScoped: false, health: { chats: { status: 'checking', lastSuccessAt: '', error: '' }, operator: { status: 'checking', lastSuccessAt: '', error: '' }, automations: { status: 'checking', lastSuccessAt: '', error: '' } } }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // Nothing has answered yet: the summary says so and no empty state claims
    // the sources are clear.
    expect(canvas.getByTestId('inbox-empty-checking').textContent).toContain('Checking your sources')
    expect(canvasElement.textContent).not.toContain('all caught up')
    const sources = canvas.getByRole('list', { name: 'Source status' })
    expect(sources.querySelectorAll('[data-testid^="source-"]')).toHaveLength(4)
    expect(sources.textContent).toContain('Chats: checking...')
    expect(sources.textContent).toContain('Update tasks: checking...')
    // A check in flight has nothing to retry.
    expect(sources.querySelectorAll('button')).toHaveLength(0)
  },
}

export const Ready: Story = {
  render: () => renderList({ chats: 'rows' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    expect(canvas.getByTestId('inbox-summary').textContent).toContain('3 items need a look.')
    expect(canvas.getByTestId('inbox-summary').textContent).not.toContain('could not be fully checked')
    const headings = [...canvasElement.querySelectorAll('.notif-section h3')].map(headingText)
    expect(headings).toEqual(['Needs you 1', 'New replies 2'])
    // A failed chat owns one Needs-you row with the failure reason.
    const blocked = canvas.getByTestId('row-chat:storybook-blocked')
    expect(blocked.textContent).toContain('The last turn failed')
    expect(blocked.querySelector('.notif-row--attention')).not.toBeNull()
    expect(blocked.querySelector('.notif-unread')).toBeNull()
    // An unread reply keeps its dot, the words for screen readers, and its time.
    const reply = canvas.getByTestId('row-chat:storybook-reply')
    expect(reply.querySelector('.notif-unread')).not.toBeNull()
    expect(reply.textContent).toContain('Unread')
    expect(reply.querySelector('time')!.getAttribute('datetime')).toBe('2026-11-18T10:00:00.000Z')
    expect((reply.querySelector('a') as HTMLAnchorElement).getAttribute('href')).toBe('/chat/storybook-reply')
    // All sources answered: no health list.
    expect(canvas.queryByRole('list', { name: 'Source status' })).toBeNull()
  },
}

export const Stale: Story = {
  render: () => renderList({ chats: 'rows', health: { chats: stale } }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    // Retained rows stay, but the summary and each row say they are not current.
    expect(canvas.getByTestId('inbox-summary').textContent).toContain('could not be fully checked')
    expect(canvas.getByTestId('row-chat:storybook-blocked').querySelector('.notif-stale')!.textContent).toBe('May be out of date')
    const source = canvas.getByTestId('source-chats')
    expect(source.textContent).toContain('showing the last successful check')
    const retry = source.querySelector('button') as HTMLButtonElement
    expect(retry.getAttribute('aria-label')).toBe('Retry checking Chats')
    expect(retry.disabled).toBe(false)
    expect(canvasElement.querySelectorAll('.notif-stale').length).toBeGreaterThan(0)
  },
}

export const Unavailable: Story = {
  render: () => renderList({ health: { chats: unavailable } }),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    // A source that never answered is never reported as "all caught up".
    expect(canvas.getByTestId('inbox-empty-unavailable').textContent).toContain('could not be checked')
    expect(canvas.queryByTestId('inbox-empty-clear')).toBeNull()
    const source = canvas.getByTestId('source-chats')
    expect(source.textContent).toContain('Chats: could not be checked.')
    const retry = source.querySelector('button') as HTMLButtonElement
    expect(retry.getAttribute('aria-label')).toBe('Retry checking Chats')
    expect(retry.disabled).toBe(false)
  },
}

export const AllClear: Story = {
  render: () => renderList({}),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    expect(canvas.getByTestId('inbox-empty-clear').textContent).toBe('You\'re all caught up.')
    expect(canvas.queryByRole('list', { name: 'Source status' })).toBeNull()
    expect(canvas.queryAllByRole('heading', { level: 2 })).toHaveLength(0)
    expect(canvas.queryAllByRole('heading', { level: 3 })).toHaveLength(0)
  },
}

export const EmptyStale: Story = {
  render: () => renderList({ health: { operator: stale } }),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    // Nothing was found last time, but one source is out of date: not clear.
    expect(canvas.getByTestId('inbox-empty-stale').textContent).toContain('Nothing was found at the last check')
    expect(canvas.queryByTestId('inbox-empty-clear')).toBeNull()
    expect(canvas.getByTestId('source-operator').textContent).toContain('Settings: showing the last successful check')
  },
}

export const RetryBusy: Story = {
  render: () => renderList({ health: { operator: unavailable } }),
  parameters: { fixture: { routes: inboxRoutes({ operator: 'pending' }) } },
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const retry = canvas.getByRole('button', { name: 'Retry checking Settings' }) as HTMLButtonElement
    expect(retry.disabled).toBe(false)
    await userEvent.click(retry)
    // The read is in flight: every retry is blocked until it answers, so a
    // second click cannot start a second request.
    await waitFor(() => expect(retry.disabled).toBe(true))
    expect(canvas.getByTestId('story-ledger').textContent).toContain('Fixture requests: 1')
    expect(canvas.queryByTestId('story-violation')).toBeNull()
  },
}

export const RowGone: Story = {
  render: () => renderList({ chats: 'rows' }),
  parameters: { fixture: { routes: inboxRoutes({ chatsRead: 'gone' }) } },
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const before = storyRoute(canvas)
    const row = canvas.getByTestId('row-chat:storybook-blocked').querySelector('a') as HTMLElement
    await userEvent.click(row)
    // The row is re-read before it opens; a chat that vanished leaves the row
    // out and says so instead of opening a dead route.
    await waitFor(() => expect(canvas.queryByTestId('row-chat:storybook-blocked')).toBeNull())
    const notice = canvas.getByTestId('inbox-unavailable')
    expect(notice.getAttribute('role')).toBe('status')
    expect(notice.textContent).toContain('no longer available')
    expect(document.activeElement).toBe(notice)
    expect(storyRoute(canvas)).toBe(before)
    expect(canvas.getByTestId('story-ledger').textContent).toContain('Fixture requests: 2')
  },
}

export const RowNavigates: Story = {
  render: () => renderList({ operator: 'one' }),
  play: async ({ canvas, userEvent }) => {
    await assertQuietFence(canvas)
    const row = canvas.getByTestId('row-operator:shared:synthetic-check-1').querySelector('a') as HTMLElement
    expect(row.getAttribute('href')).toContain('/?notice=')
    await userEvent.click(row)
    // A row without its own re-read navigates directly and tells the bell to close.
    await waitFor(() => expect(storyRoute(canvas)).toContain('notice='))
    expect(storyRoute(canvas)).toContain('action%3Ashared%3Asynthetic-check-1')
    expect(canvas.getByTestId('row-operator:shared:synthetic-check-1')).toBeTruthy()
  },
}

export const Sheet: Story = {
  render: () => renderList({ chats: 'rows' }, { density: 'sheet' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    expect(canvasElement.querySelector('.notif-inbox')!.classList.contains('notif-inbox--sheet')).toBe(true)
    const row = canvasElement.querySelector('.notif-row') as HTMLElement
    // The sheet is designed around a larger target than the popover.
    expect(getComputedStyle(row).minHeight).toBe('52px')
  },
}

export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  render: () => renderList({ chats: 'rows' }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const root = canvasElement.querySelector('.notif-inbox') as HTMLElement
    expect(root.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    for (const row of canvasElement.querySelectorAll('.notif-row')) {
      expect(row.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    }
    // The row still stacks its title, reason, time and state inside the width.
    expect(canvas.getByTestId('inbox-summary').getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
