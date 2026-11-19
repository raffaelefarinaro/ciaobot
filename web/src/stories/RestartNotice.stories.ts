import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, fireEvent, waitFor } from 'storybook/test'
import { defineComponent, h, onBeforeUnmount } from 'vue'
import RestartNotice from '../components/RestartNotice.vue'
import { DEFAULT_RESTART_MESSAGE, MAX_RESTART_MESSAGE_CHARS, restartMessageForDisplay } from '../lib/serverRestart'

// The fixed card App.vue shows while the engine drains for a restart. It stays
// draggable and remembers where it was dropped, so the states are the message
// variants plus the two ways of moving it.
const POS_KEY = 'ciao:restart-notice-pos'
const SPINNER_FRAMES = ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏']

/** The long message the engine caps before it reaches the card. */
const LONG_MESSAGE = restartMessageForDisplay('Synthetic restart detail that keeps going '.repeat(12))

/**
 * A stored position from an earlier session would otherwise leak into the next
 * story in the same runner page: clear it before the card reads it, and put the
 * one this story needs back.
 */
function renderCard(message?: string, storedPos?: { x: number, y: number }) {
  return defineComponent({
    setup() {
      localStorage.removeItem(POS_KEY)
      if (storedPos) localStorage.setItem(POS_KEY, JSON.stringify(storedPos))
      onBeforeUnmount(() => localStorage.removeItem(POS_KEY))
      return () => h('div', [h(RestartNotice, message ? { message } : {})])
    },
  })
}

const meta = {
  id: 'restart-notice',
  title: 'Notices and notifications/RestartNotice',
  component: RestartNotice,
  tags: ['!autodocs'],
} satisfies Meta<typeof RestartNotice>
export default meta
type Story = StoryObj<typeof meta>

function cardOf(canvasElement: HTMLElement): HTMLElement {
  return canvasElement.querySelector('.restart-card') as HTMLElement
}

/** Wait out the entrance animation so a measured box is the resting one. */
async function settled(card: HTMLElement): Promise<void> {
  await Promise.all(card.getAnimations().map((animation) => animation.finished))
}

export const Default: Story = {
  render: () => renderCard(),
  play: async ({ canvas, canvasElement }) => {
    const card = cardOf(canvasElement)
    const status = canvas.getByRole('status', { name: 'Restart status. Drag, or use the arrow keys, to move this card.' })
    expect(status).toBe(card)
    // Polite, not an alert: a restart is background news the user keeps orienting against.
    expect(card.getAttribute('aria-live')).toBe('polite')
    expect(card.getAttribute('tabindex')).toBe('0')
    expect(card.getAttribute('title')).toBe('Drag to move')
    expect(canvas.getByText(DEFAULT_RESTART_MESSAGE)).toBeTruthy()
    expect(canvas.getByText('restart')).toBeTruthy()
    expect(canvas.getByText('waiting for server')).toBeTruthy()
    // The spinner is a live frame from the card's own animation, not decoration.
    expect(SPINNER_FRAMES).toContain((canvasElement.querySelector('.restart-spinner') as HTMLElement).textContent)
    expect(card.classList.contains('restart-card--moved')).toBe(false)
    // No remembered position means the stylesheet owns where it sits.
    expect(card.style.left).toBe('')
    expect(card.style.top).toBe('')
  },
}

export const CustomMessage: Story = {
  render: () => renderCard('Synthetic restart: the workspace index is reloading.'),
  play: async ({ canvas }) => {
    expect(canvas.getByText('Synthetic restart: the workspace index is reloading.')).toBeTruthy()
    expect(canvas.queryByText(DEFAULT_RESTART_MESSAGE)).toBeNull()
  },
}

export const LongMessage: Story = {
  render: () => renderCard(LONG_MESSAGE),
  play: async ({ canvas, canvasElement }) => {
    const message = canvas.getByText(LONG_MESSAGE) as HTMLElement
    // The card shows what `restartMessageForDisplay` already bounded, and the
    // 3-line clamp is the visual backstop on top of that.
    expect(LONG_MESSAGE.length).toBeLessThanOrEqual(MAX_RESTART_MESSAGE_CHARS)
    expect(LONG_MESSAGE.endsWith('…')).toBe(true)
    expect(message.scrollWidth).toBeLessThanOrEqual(message.clientWidth + 1)
    await waitFor(() => expect(message.scrollHeight).toBeGreaterThan(message.clientHeight))
    expect(cardOf(canvasElement).getBoundingClientRect().width).toBeLessThanOrEqual(400)
  },
}

export const Moved: Story = {
  render: () => renderCard(undefined, { x: 40, y: 50 }),
  play: async ({ canvasElement }) => {
    const card = cardOf(canvasElement)
    expect(card.classList.contains('restart-card--moved')).toBe(true)
    expect(card.style.left).toBe('40px')
    expect(card.style.top).toBe('50px')
    expect(card.style.bottom).toBe('auto')
    expect(card.style.transform).toBe('none')
    // Where it was dropped is where the next visit finds it.
    expect(JSON.parse(localStorage.getItem(POS_KEY)!)).toEqual({ x: 40, y: 50 })
  },
}

export const KeyboardMove: Story = {
  render: () => renderCard(),
  play: async ({ canvasElement }) => {
    const card = cardOf(canvasElement)
    await settled(card)
    const before = card.getBoundingClientRect()
    card.focus()
    expect(document.activeElement).toBe(card)
    fireEvent.keyDown(card, { key: 'ArrowUp' })
    await waitFor(() => expect(card.classList.contains('restart-card--moved')).toBe(true))
    const moved = card.getBoundingClientRect()
    expect(moved.top).toBeLessThan(before.top)
    expect(moved.top).toBeCloseTo(before.top - 16, 0)
    expect(moved.left).toBeCloseTo(before.left, 0)
    // Focus stays on the card that just moved, and the drop is remembered.
    expect(document.activeElement).toBe(card)
    expect(JSON.parse(localStorage.getItem(POS_KEY)!)).toEqual({
      x: Number.parseFloat(card.style.left),
      y: Number.parseFloat(card.style.top),
    })
    fireEvent.keyDown(card, { key: 'ArrowDown' })
    await waitFor(() => expect(card.getBoundingClientRect().top).toBeCloseTo(before.top, 0))
  },
}

export const Drag: Story = {
  render: () => renderCard(),
  play: async ({ canvasElement }) => {
    const card = cardOf(canvasElement)
    await settled(card)
    const before = card.getBoundingClientRect()
    fireEvent.pointerDown(card, {
      button: 0, pointerType: 'mouse', pointerId: 1,
      clientX: before.left + 30, clientY: before.top + 20,
    })
    await waitFor(() => expect(card.classList.contains('restart-card--dragging')).toBe(true))
    fireEvent.pointerMove(card, {
      pointerType: 'mouse', pointerId: 1,
      clientX: before.left + 70, clientY: before.top,
    })
    await waitFor(() => expect(card.getBoundingClientRect().left).toBeCloseTo(before.left + 40, 0))
    const during = card.getBoundingClientRect()
    expect(during.top).toBeCloseTo(before.top - 20, 0)
    fireEvent.pointerUp(card, { pointerType: 'mouse', pointerId: 1 })
    await waitFor(() => expect(card.classList.contains('restart-card--dragging')).toBe(false))
    expect(card.classList.contains('restart-card--moved')).toBe(true)
    const stored = JSON.parse(localStorage.getItem(POS_KEY)!) as { x: number, y: number }
    expect(Math.round(stored.x)).toBe(Math.round(during.left))
    expect(Math.round(stored.y)).toBe(Math.round(during.top))
  },
}
