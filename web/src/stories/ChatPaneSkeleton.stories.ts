import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import ChatPaneSkeleton from '../components/ChatPaneSkeleton.vue'

// The prop-less placeholder a chat pane draws while its lazy chunk or catalog
// entry is on the way. It has one render and no args: the only states are the
// loading render and the narrow pane it is drawn in.
const meta = {
  id: 'chat-pane-skeleton',
  title: 'Skeletons/ChatPaneSkeleton',
  component: ChatPaneSkeleton,
} satisfies Meta<typeof ChatPaneSkeleton>
export default meta
type Story = StoryObj<typeof meta>

export const Loading: Story = {
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading conversation' })
    expect(status.getAttribute('aria-busy')).toBe('true')
    expect(status.getAttribute('aria-live')).toBe('polite')
    // The pane header the surface grows into, then the transcript's own shapes:
    // assistant bubble, user bubble, collapsed trace row, assistant bubble.
    expect(canvasElement.querySelectorAll('.pane-skeleton-line--title')).toHaveLength(1)
    expect(canvasElement.querySelectorAll('.pane-skeleton-msg--assistant')).toHaveLength(2)
    expect(canvasElement.querySelectorAll('.pane-skeleton-msg--user')).toHaveLength(1)
    expect(canvasElement.querySelectorAll('.pane-skeleton-trace')).toHaveLength(1)
    for (const shape of canvasElement.querySelectorAll('.pane-skeleton-header, .pane-skeleton-msg, .pane-skeleton-trace')) {
      expect(shape.getAttribute('aria-hidden')).toBe('true')
    }
    // No interactive surface at all: the placeholder cannot take focus or a click.
    expect(canvasElement.querySelectorAll('a, button, input, textarea, select, [tabindex], [contenteditable]')).toHaveLength(0)
  },
}

// A 390px containing block stands in for the narrow pane. The body keeps its
// reading measure and stays inside the block; the phone header grid itself is a
// max-width 768px viewport media query, so it needs a real narrow viewport.
export const Narrow: Story = {
  decorators: [() => ({ template: '<div style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas, canvasElement }) => {
    const status = canvas.getByRole('status', { name: 'Loading conversation' })
    expect(status.getAttribute('aria-busy')).toBe('true')
    const body = canvasElement.querySelector('.pane-skeleton-body')
    expect(body).not.toBeNull()
    expect(status.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(body!.getBoundingClientRect().width).toBeLessThanOrEqual(390)
  },
}
