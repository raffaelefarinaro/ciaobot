import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, fireEvent, waitFor } from 'storybook/test'
import InAppToast from '../components/InAppToast.vue'
import {
  assertQuietFence,
  connectedOwnerStory,
  seedToasts,
  storyRoute,
  toast,
} from './notificationInboxFixture'
import type { InAppToast as Toast } from '../lib/types'

// The app's toast stack, rendered from the real project store. Every fixture is
// synthetic, and the three connected states only take actions the store can
// complete locally (dismiss, action callback, router push); anything that would
// create a chat is a fixture violation, not a silent network call.
const INFO = 'Synthetic notice'
const INFO_BODY = 'A synthetic notice for the toast stack.'
const OTHER = 'Synthetic other-workspace notice'

function renderStack(toasts: Toast[], prepare?: () => () => void) {
  return connectedOwnerStory({
    component: InAppToast,
    seed: (pinia) => seedToasts(pinia, toasts),
    label: 'in-app toast',
    prepare,
  })
}

const meta = {
  id: 'in-app-toast',
  title: 'Notices and notifications/InAppToast',
  component: InAppToast,
  tags: ['!autodocs'],
} satisfies Meta<typeof InAppToast>
export default meta
type Story = StoryObj<typeof meta>

const toastOf = (canvasElement: HTMLElement) =>
  canvasElement.querySelector('.toast') as HTMLElement

export const Info: Story = {
  render: () => renderStack([toast(1, { chat_id: '', title: INFO, body: INFO_BODY })]),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const item = toastOf(canvasElement)
    // A div cannot take its name from its contents, so the role is the shape
    // and the text is checked directly.
    expect(item.getAttribute('role')).toBe('status')
    expect(item.classList.contains('toast-error')).toBe(false)
    expect(canvas.getByText(INFO)).toBeTruthy()
    expect(canvas.getByText(INFO_BODY)).toBeTruthy()
    // An info toast with nothing to offer is just text plus a way out.
    expect(canvas.queryByRole('button', { name: 'Open' })).toBeNull()
    expect(canvas.getByRole('button', { name: 'Dismiss' })).toBeTruthy()
  },
}

export const Action: Story = {
  render: () => renderStack([
    toast(1, { chat_id: '', title: INFO, body: INFO_BODY, actionLabel: 'Review', onAction: () => { document.body.dataset.storyAction = 'ran' } }),
  ], () => () => { delete document.body.dataset.storyAction }),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await assertQuietFence(canvas)
    const action = canvas.getByRole('button', { name: 'Review' })
    expect(action.classList.contains('toast-action')).toBe(true)
    await userEvent.click(action)
    // The action ran and the toast it belonged to is gone.
    expect(document.body.dataset.storyAction).toBe('ran')
    await waitFor(() => expect(canvasElement.querySelector('.toast')).toBeNull())
  },
}

export const Link: Story = {
  render: () => renderStack([
    toast(1, { chat_id: '', title: INFO, body: INFO_BODY, linkLabel: 'What\'s new', linkUrl: 'https://storybook.invalid/releases' }),
  ]),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    const link = canvas.getByRole('link', { name: 'What\'s new' }) as HTMLAnchorElement
    expect(link.getAttribute('href')).toBe('https://storybook.invalid/releases')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
    expect(link.classList.contains('toast-link')).toBe(true)
  },
}

export const Error: Story = {
  render: () => renderStack([
    toast(1, { chat_id: '', variant: 'error', title: 'Synthetic failure', body: 'A synthetic turn failed.' }),
  ]),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await assertQuietFence(canvas)
    // Errors interrupt: the toast is an alert, not a polite status.
    const item = toastOf(canvasElement)
    expect(item.getAttribute('role')).toBe('alert')
    expect(item.classList.contains('toast-error')).toBe(true)
    expect(item.textContent).toContain('Synthetic failure')
    expect(canvas.getByRole('button', { name: 'Fix this error' })).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(canvasElement.querySelector('.toast')).toBeNull())
  },
}

export const FixInSettings: Story = {
  render: () => renderStack([
    toast(1, {
      chat_id: '', variant: 'error', title: 'Synthetic account alert', body: 'A synthetic account needs attention.',
      fixRoute: '/settings', fixLabel: 'Review in Settings',
    }),
  ]),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await assertQuietFence(canvas)
    await userEvent.click(canvas.getByRole('button', { name: 'Review in Settings' }))
    // A toast whose fix lives in Settings routes there and does not seed a chat.
    await waitFor(() => expect(storyRoute(canvas)).toContain('/settings'))
    await waitFor(() => expect(canvasElement.querySelector('.toast')).toBeNull())
  },
}

export const RestoreDraft: Story = {
  render: () => renderStack([
    toast(1, {
      chat_id: '', variant: 'error', title: 'Synthetic recovered draft',
      body: 'A synthetic draft can be restored.',
      restoreDraft: { originalChatId: 'storybook-draft', projectId: 'storybook-p1', text: 'Synthetic draft text' },
    }),
  ]),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await assertQuietFence(canvas)
    // Restoring creates a chat through the store, so that click is left to the
    // component tests; this state proves the label and the way out.
    expect(canvas.getByRole('button', { name: 'Restore draft' })).toBeTruthy()
    await userEvent.click(canvas.getByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(canvasElement.querySelector('.toast')).toBeNull())
  },
}

export const Dismiss: Story = {
  render: () => renderStack([toast(1, { chat_id: '', title: INFO, body: INFO_BODY })]),
  play: async ({ canvas, canvasElement, userEvent }) => {
    await assertQuietFence(canvas)
    const close = canvas.getByRole('button', { name: 'Dismiss' })
    expect(close.closest('.toast')).toBe(toastOf(canvasElement))
    await userEvent.click(close)
    // The stack itself goes away with its last toast.
    await waitFor(() => expect(canvasElement.querySelector('.toast-stack')).toBeNull())
  },
}

export const Swipe: Story = {
  render: () => renderStack([toast(1, { chat_id: '', title: INFO, body: INFO_BODY })]),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const item = toastOf(canvasElement)
    fireEvent.pointerDown(item, {
      button: 0, pointerType: 'mouse', pointerId: 1, clientX: 100, clientY: 40,
    })
    fireEvent.pointerMove(item, { pointerType: 'mouse', pointerId: 1, clientX: 190, clientY: 40 })
    await waitFor(() => expect(item.classList.contains('toast-swiping')).toBe(true))
    fireEvent.pointerUp(item, { pointerType: 'mouse', pointerId: 1, clientX: 190, clientY: 40 })
    // Past the 80px threshold the gesture dismisses instead of opening the chat.
    await waitFor(() => expect(canvasElement.querySelector('.toast')).toBeNull())
  },
}

export const UnderModal: Story = {
  render: () => renderStack(
    [toast(1, { chat_id: '', title: INFO, body: INFO_BODY })],
    () => {
      const dialog = document.createElement('div')
      dialog.setAttribute('aria-modal', 'true')
      dialog.setAttribute('role', 'dialog')
      dialog.setAttribute('aria-label', 'Synthetic open dialog')
      document.body.appendChild(dialog)
      return () => dialog.remove()
    },
  ),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const stack = canvasElement.querySelector('.toast-stack') as HTMLElement
    // Behind an open dialog the toast recedes instead of painting over it.
    expect(stack.classList.contains('toast-stack--under-modal')).toBe(true)
    expect(getComputedStyle(stack).zIndex).toBe('99')
  },
}

export const WorkspaceScope: Story = {
  render: () => renderStack([
    toast(1, { chat_id: '', title: INFO, body: INFO_BODY }),
    toast(2, { chat_id: 'storybook-other-workspace', title: OTHER, body: 'Belongs to another workspace.' }),
  ]),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    expect(canvasElement.querySelectorAll('.toast')).toHaveLength(1)
    expect(canvas.getByText(INFO)).toBeTruthy()
    expect(canvas.queryByText(OTHER)).toBeNull()
  },
}
