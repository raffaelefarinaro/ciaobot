import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import Wordmark from '../components/Wordmark.vue'

const meta = {
  id: 'wordmark',
  title: 'Brand and glyphs/Wordmark',
  component: Wordmark,
  args: { size: 'md' },
} satisfies Meta<typeof Wordmark>
export default meta
type Story = StoryObj<typeof meta>

// The supplied wordmark artwork: one image whose source follows the theme
// (the theme-light class on <html>). The accessible name is always Ciaobot.
export const Default: Story = {
  play: async ({ canvas }) => {
    const mark = canvas.getByRole('img', { name: 'Ciaobot' })
    expect(mark.tagName).toBe('IMG')
    expect(mark.classList.contains('wordmark--md')).toBe(true)
    expect(mark.getAttribute('src')).toMatch(/brand\/ciaobot-wordmark(-light)?\.png$/)
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

// The brand image in a 390px column: it stays on one line and fits the narrow shell.
export const Narrow: Story = {
  decorators: [() => ({ template: '<div data-story-narrow style="max-width: 390px"><story /></div>' })],
  play: async ({ canvas, canvasElement }) => {
    const mark = canvas.getByRole('img', { name: 'Ciaobot' })
    const box = canvasElement.querySelector('[data-story-narrow]')
    expect(mark.classList.contains('wordmark--md')).toBe(true)
    expect(box?.getBoundingClientRect().width).toBeLessThanOrEqual(390)
    expect(mark.getBoundingClientRect().width).toBeLessThanOrEqual(box?.getBoundingClientRect().width ?? 0)
    // max-height keeps the artwork's ratio: it is never squashed in the narrow row.
    expect(mark.getBoundingClientRect().height).toBeLessThanOrEqual(22)
  },
}
