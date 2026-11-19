import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import CommentComposePopover from '../components/CommentComposePopover.vue'
import { assertQuietFence, renderComposeState } from './pinsFixture'

/**
 * The shared floating compose popover (#3303, slice B18): the production
 * CommentComposePopover.vue mounts inside `mountConnected` with a seeded
 * project store (its workspace accent only) and no routes — the component
 * attempts no request. It teleports to <body> unless `inline`, so plays assert
 * against the document; the host's visible event log records what the popover
 * emitted for save/cancel/remove flows, since the parent owns every close.
 */

const meta = {
  id: 'comment-compose-popover',
  title: 'Pins and comments/CommentComposePopover',
  component: CommentComposePopover,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof CommentComposePopover>
export default meta
type Story = StoryObj

const SB_ANCHOR = { top: 120, left: 120 }

/** What the host's event log, a visible line under the fixture, records now. */
function eventsLine(): string {
  return document.querySelector('[data-testid="story-events"]')?.textContent ?? 'missing events line'
}

export const Default: Story = {
  render: () => renderComposeState('empty draft anchored on a selection', { anchor: SB_ANCHOR }),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    const compose = await waitFor(() => screen.getByRole('dialog', { name: 'Add comment' }))
    // Opening takes the caret, so typing belongs to the note at once.
    const textarea = compose.querySelector('textarea') as HTMLTextAreaElement
    await waitFor(() => expect(document.activeElement).toBe(textarea))
    expect(textarea.getAttribute('placeholder')).toBe('Add a note for Ciao')
    expect(textarea.value).toBe('')
    // An empty draft is actually blocked, not styled.
    expect(screen.getByRole('button', { name: 'Add comment' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(compose.querySelector('.compose-images')).toBeNull()
  },
}

export const Draft: Story = {
  render: () => renderComposeState('a typed draft saved from the popover', { anchor: SB_ANCHOR }),
  play: async ({ canvas, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Add comment' })).toBeTruthy())
    const textarea = screen.getByRole('dialog', { name: 'Add comment' }).querySelector('textarea') as HTMLTextAreaElement
    await event.type(textarea, 'A synthetic note')
    const add = screen.getByRole('button', { name: 'Add comment' })
    await waitFor(() => expect(add.hasAttribute('disabled')).toBe(false))
    await event.click(add)
    // The save is emitted for the parent to commit; the popover stays open
    // until the parent clears its anchor, which this fixture leaves alone.
    expect(eventsLine()).toContain('input:A synthetic note')
    expect(eventsLine()).toContain('save')
  },
}

export const Images: Story = {
  render: () => renderComposeState('a draft carrying attached images', {
    anchor: SB_ANCHOR,
    images: ['sb-img-a', 'sb-img-b'],
  }),
  play: async ({ canvas, userEvent: event }) => {
    await assertQuietFence(canvas)
    const compose = await waitFor(() => screen.getByRole('dialog', { name: 'Add comment' }))
    const thumbs = compose.querySelectorAll('.compose-image')
    expect(thumbs.length).toBe(2)
    expect(thumbs[0]?.querySelector('img')?.getAttribute('src')).toBe('/api/images/sb-img-a')
    // The remove control emits the index for the parent to drop.
    await event.click(thumbs[0]?.querySelector('.compose-image-remove') as HTMLElement)
    expect(eventsLine()).toContain('removeImage:0')
  },
}

export const Inline: Story = {
  render: () => renderComposeState('the dialog-inline variant', { anchor: SB_ANCHOR, inline: true }),
  play: async ({ canvas, canvasElement }) => {
    await assertQuietFence(canvas)
    const compose = await waitFor(() => canvasElement.querySelector('.compose') as HTMLElement | null)
    expect(compose?.getAttribute('role')).toBe('dialog')
    expect(compose?.getAttribute('aria-label')).toBe('Add comment')
    // Inline renders in the dialog's DOM instead of teleporting to <body>.
    expect(document.querySelector('body > .compose')).toBeNull()
    expect(compose?.querySelector('textarea')).toBeTruthy()
  },
}

export const Keyboard: Story = {
  render: () => renderComposeState('the keyboard paths of the compose popover', { anchor: SB_ANCHOR }),
  play: async ({ canvas, userEvent: event }) => {
    await assertQuietFence(canvas)
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Add comment' })).toBeTruthy())
    const textarea = screen.getByRole('dialog', { name: 'Add comment' }).querySelector('textarea') as HTMLTextAreaElement
    await event.type(textarea, 'Keyboard note')
    // ⌘/Ctrl+Enter saves through the popover's own keydown handler.
    await event.keyboard('{Control>}{Enter}')
    expect(eventsLine()).toContain('save')
    // Escape cancels through the same handler.
    await event.keyboard('{Escape}')
    expect(eventsLine()).toContain('cancel')
    // The box is still anchored; closing is the parent's job, and this fixture
    // leaves the anchor alone so both paths are observable.
    expect(screen.getByRole('dialog', { name: 'Add comment' })).toBeTruthy()
  },
}

export const Narrow: Story = {
  // The popover is a fixed-position overlay teleported to <body>, so the
  // narrow viewport is the story viewport, not a wrapper; its phone placement
  // (~640px media rule) is viewport-driven.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  render: () => renderComposeState('the compose popover in a 390px viewport', { anchor: { top: 120, left: 40 } }),
  play: async ({ canvas }) => {
    await assertQuietFence(canvas)
    const compose = await waitFor(() => screen.getByRole('dialog', { name: 'Add comment' }))
    const rect = compose.getBoundingClientRect()
    // The phone placement pins it to the 8px gutters across the viewport width.
    expect(rect.left).toBeGreaterThanOrEqual(7)
    expect(rect.width).toBeGreaterThan(300)
    expect(Math.abs(rect.width - (window.innerWidth - 16))).toBeLessThanOrEqual(4)
    expect(compose.querySelector('textarea')).toBeTruthy()
  },
}
