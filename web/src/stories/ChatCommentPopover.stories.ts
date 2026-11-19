import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen, waitFor } from 'storybook/test'
import { defineComponent, h, ref } from 'vue'
import ChatCommentPopover from '../components/ChatCommentPopover.vue'

/**
 * The transcript read popover (#3303, slice B18): ChatCommentPopover.vue owns
 * its own popover state and is driven imperatively by ChatPanel through its
 * exposed handlers, so each story renders the production component behind a
 * tiny host with the highlight spans the transcript would render, and the
 * plays act through those handlers exactly as the app binds them at event
 * time. Props only: no stores, no transport; the popover teleports to <body>,
 * so plays assert against the document, and the preview boundary keeps the
 * fence quiet (the component attempts no request).
 */

type HostComment = { id: string, comment: string, selection?: string }

const SB_DRAFT_ID = '__draft__'
const SB_COMMENT_A: HostComment = { id: 'sb-c-1', comment: 'A synthetic first note.', selection: 'Synthetic selected text' }

/** A span standing in for a commented highlight the transcript renders. */
function highlightStyle(): string {
  return 'display: inline-block; padding: 2px 6px; background: var(--bg3); border-radius: 4px'
}

function hoverEvent(target: HTMLElement): MouseEvent {
  const e = new MouseEvent('mouseover', { bubbles: true })
  Object.defineProperty(e, 'target', { value: target, configurable: true })
  return e
}

/** What the host's event log, a visible line under the popover, records now. */
function eventsLine(): string {
  return document.querySelector('[data-testid="story-events"]')?.textContent ?? 'missing events line'
}

/**
 * The host: renders the highlights, binds the same exposed handlers ChatPanel
 * binds at event time, and records the emitted edits and deletions in a log
 * line, so the plays can assert handoffs without owning any state. The second
 * span always carries the draft id, which the popover must ignore.
 */
function renderPopoverHost(comments: HostComment[]) {
  return defineComponent({
    name: 'ChatCommentPopoverHost',
    setup() {
      const pop = ref<InstanceType<typeof ChatCommentPopover>>()
      const hl1 = ref<HTMLElement>()
      const hlDraft = ref<HTMLElement>()
      const log = ref<string[]>([])
      function record(entry: string): void { log.value = [...log.value, entry] }
      return () => [
        h('p', 'Synthetic transcript highlights for the production popover.'),
        h('div', { style: 'min-height: 90px; padding: 12px' }, [
          h('span', { class: 'comment-highlight', 'data-comment-id': SB_COMMENT_A.id, 'ref': hl1, style: highlightStyle() }, 'synthetic highlight one'),
          h('span', { class: 'comment-highlight', 'data-comment-id': SB_DRAFT_ID, 'ref': hlDraft, style: highlightStyle() }, 'synthetic draft highlight'),
        ]),
        h('button', {
          type: 'button',
          'data-testid': 'story-hover',
          onClick: () => pop.value?.onTargetOver(hoverEvent(hl1.value!)),
        }, 'Hover highlight'),
        h('button', {
          type: 'button',
          'data-testid': 'story-pin',
          onClick: () => {
            const id = pop.value?.pinFromEvent(hoverEvent(hl1.value!)) ?? null
            record(`pin:${id ?? 'none'}`)
          },
        }, 'Pin highlight'),
        h('button', {
          type: 'button',
          'data-testid': 'story-hover-draft',
          onClick: () => pop.value?.onTargetOver(hoverEvent(hlDraft.value!)),
        }, 'Hover draft highlight'),
        h(ChatCommentPopover, {
          'comments': comments,
          'draftId': SB_DRAFT_ID,
          'onEdit': (c: HostComment) => record(`edit:${c.id}`),
          'onDelete': (id: string) => record(`delete:${id}`),
          'ref': pop,
        }),
        h('p', { 'data-testid': 'story-events' }, `Events: ${log.value.join(' | ') || 'none'}`),
      ]
    },
  })
}

const meta = {
  id: 'chat-comment-popover',
  title: 'Pins and comments/ChatCommentPopover',
  component: ChatCommentPopover,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof ChatCommentPopover>
export default meta
type Story = StoryObj

async function pinnedPopover(event: {
  click: (el: HTMLElement) => Promise<void>
}): Promise<void> {
  await event.click(document.querySelector('[data-testid="story-pin"]') as HTMLElement)
  await waitFor(() => expect(screen.getByRole('dialog', { name: 'Comment' })).toBeTruthy())
}

export const Default: Story = {
  render: () => renderPopoverHost([SB_COMMENT_A]),
  play: async ({ canvasElement }) => {
    // A closed popover renders nothing: no dialog, no backdrop, no events.
    await waitFor(() => expect(canvasElement.querySelector('.comment-highlight')).toBeTruthy())
    expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull()
    expect(document.querySelector('.pop-backdrop')).toBeNull()
    expect(canvasElement.querySelector('[data-testid="story-events"]')?.textContent).toBe('Events: none')
  },
}

export const HoverPreview: Story = {
  render: () => renderPopoverHost([SB_COMMENT_A]),
  play: async ({ canvas, userEvent: event }) => {
    await event.click(canvas.getByTestId('story-hover'))
    const popover = await waitFor(() => screen.getByRole('dialog', { name: 'Comment' }))
    // A hover preview shows the quoted selection and the note, and is not pinned.
    expect(popover.textContent).toContain('“Synthetic selected text”')
    expect(popover.textContent).toContain(SB_COMMENT_A.comment)
    expect(popover.querySelector('.pop-btn-edit')).toBeTruthy()
    expect(popover.querySelector('.pop-btn-remove')).toBeTruthy()
    expect(document.querySelector('.pop-backdrop')).toBeNull()
    // Nothing was recorded: a bare hover is informational.
    expect(eventsLine()).toBe('Events: none')
  },
}

export const Pinned: Story = {
  render: () => renderPopoverHost([SB_COMMENT_A]),
  play: async ({ canvas, userEvent: event }) => {
    void canvas
    await pinnedPopover(event)
    expect(screen.getByRole('dialog', { name: 'Comment' }).textContent).toContain(SB_COMMENT_A.comment)
    expect(eventsLine()).toBe('Events: pin:sb-c-1')
    expect(document.querySelector('.pop-backdrop')).toBeTruthy()
    // An explicit pin gives the popover focus, so keyboard users can act at once.
    await waitFor(() => expect(document.activeElement?.classList.contains('pop-btn-edit')).toBe(true))
    await event.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull())
    expect(document.querySelector('.pop-backdrop')).toBeNull()
  },
}

export const Actions: Story = {
  render: () => renderPopoverHost([SB_COMMENT_A, { id: 'sb-c-2', comment: 'A synthetic second note.' }]),
  play: async ({ canvas, userEvent: event }) => {
    // Edit closes the popover and hands the comment to the panel.
    await event.click(canvas.getByTestId('story-pin'))
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Comment' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull())
    expect(eventsLine()).toContain('edit:sb-c-1')
    // Delete also closes, naming the id to remove.
    await event.click(canvas.getByTestId('story-pin'))
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Comment' })).toBeTruthy())
    await event.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull())
    expect(eventsLine()).toContain('delete:sb-c-1')
  },
}

export const DraftIgnored: Story = {
  render: () => renderPopoverHost([SB_COMMENT_A]),
  play: async ({ canvas, userEvent: event }) => {
    // The in-flight draft highlight has no saved comment: a hover on it opens nothing.
    await event.click(canvas.getByTestId('story-hover-draft'))
    await new Promise(resolve => setTimeout(resolve, 30))
    expect(screen.queryByRole('dialog', { name: 'Comment' })).toBeNull()
    expect(eventsLine()).toBe('Events: none')
    // A genuine id beside it still previews.
    await event.click(canvas.getByTestId('story-hover'))
    await waitFor(() => expect(screen.getByRole('dialog', { name: 'Comment' })).toBeTruthy())
  },
}

export const Narrow: Story = {
  // The popover is a fixed-position overlay teleported to <body>, so the
  // narrow viewport is the story viewport, not a wrapper.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  render: () => renderPopoverHost([SB_COMMENT_A]),
  play: async ({ userEvent: event }) => {
    await pinnedPopover(event)
    const pop = screen.getByRole('dialog', { name: 'Comment' })
    const rect = pop.getBoundingClientRect()
    expect(rect.width).toBeGreaterThan(0)
    expect(rect.width).toBeLessThanOrEqual(window.innerWidth)
    expect(rect.left).toBeGreaterThanOrEqual(0)
    expect(rect.right).toBeLessThanOrEqual(window.innerWidth)
    expect(pop.textContent).toContain(SB_COMMENT_A.comment)
  },
}
