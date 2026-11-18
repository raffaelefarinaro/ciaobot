import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import Wordmark from '../components/Wordmark.vue'

const meta = {
  id: 'wordmark',
  title: 'Brand and glyphs/Wordmark',
  component: Wordmark,
  args: { text: 'Ciaobot', size: 'md' },
} satisfies Meta<typeof Wordmark>
export default meta
type Story = StoryObj<typeof meta>

// The template branches on each character: an `o` (any case) becomes an accent
// dot, every other character stays a letter. The accessible name is the whole
// text, because the letters and dots are hidden from assistive technology.
export const Default: Story = {
  play: async ({ canvas }) => {
    const mark = canvas.getByRole('img', { name: 'Ciaobot' })
    expect(mark.classList.contains('wordmark--md')).toBe(true)
    expect(mark.querySelectorAll('.wordmark-dot')).toHaveLength(2)
  },
}

export const Small: Story = {
  args: { size: 'sm' },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('img', { name: 'Ciaobot' }).classList.contains('wordmark--sm')).toBe(true)
  },
}

export const Large: Story = {
  args: { size: 'lg' },
  play: async ({ canvas }) => {
    expect(canvas.getByRole('img', { name: 'Ciaobot' }).classList.contains('wordmark--lg')).toBe(true)
  },
}

// The no-dot branch: a synthetic word without an `o`, so every character
// renders as a letter and no accent dot appears.
export const NoDots: Story = {
  args: { text: 'Skyline' },
  play: async ({ canvas }) => {
    const mark = canvas.getByRole('img', { name: 'Skyline' })
    expect(mark.querySelectorAll('.wordmark-dot')).toHaveLength(0)
    expect(mark.textContent).toBe('Skyline')
  },
}

// The brand text in a 390px column: the wordmark stays on one line and fits
// the narrow shell.
export const Narrow: Story = {
  decorators: [() => ({ template: '<div data-story-narrow style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas, canvasElement }) => {
    const mark = canvas.getByRole('img', { name: 'Ciaobot' })
    const box = canvasElement.querySelector('[data-story-narrow]')
    expect(mark.classList.contains('wordmark--md')).toBe(true)
    expect(box?.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(mark.getBoundingClientRect().width).toBeLessThanOrEqual(box?.getBoundingClientRect().width ?? 0)
  },
}
