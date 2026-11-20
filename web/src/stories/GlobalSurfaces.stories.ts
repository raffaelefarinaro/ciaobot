// Storybook for the global surface family: the production `.card` class from
// `web/src/styles/app.css` rendered on a native element, never a copy of the
// CSS. The stylesheet import is the owner link the inventory coverage guard
// checks, and token assertions read through inert probe elements.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-surfaces',
  title: 'Global CSS families/Global surfaces',
  tags: ['!autodocs'],
} satisfies Meta
export default meta
type Story = StoryObj<typeof meta>

const probe = (name: string, style: string): string =>
  `<span aria-hidden="true" data-probe="${name}" style="position: absolute; width: 0; height: 0; overflow: hidden; pointer-events: none; ${style}"></span>`

const probeOf = (canvasElement: HTMLElement, name: string): HTMLElement => {
  const found = canvasElement.querySelector(`[data-probe="${name}"]`)
  if (!found) throw new Error(`Missing probe: ${name}`)
  return found as HTMLElement
}

// The family baseline: an unclassed block, the surface the card classes paint.
export const Default: Story = {
  render: () => ({ template: '<div>Storybook unclassed block</div>' }),
  play: async ({ canvas }) => {
    const block = canvas.getByText('Storybook unclassed block')
    const style = getComputedStyle(block)
    expect(style.display).toBe('block')
    expect(style.backgroundColor).toBe('rgba(0, 0, 0, 0)')
    expect(style.borderTopStyle).toBe('none')
  },
}

// The surface contract: a --bg2 panel with a 1px --border edge and --radius
// corners, laid out as a flex column with 12px gaps and 16px padding.
export const Card: Story = {
  render: () => ({
    template: [
      '<div class="card"><p>Storybook card body</p></div>',
      probe('fill', 'background: var(--bg2)'),
      probe('edge', 'border-color: var(--border)'),
      probe('corner', 'border-radius: var(--radius)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const card = canvas.getByText('Storybook card body').closest('.card') as HTMLElement
    const style = getComputedStyle(card)
    expect(style.display).toBe('flex')
    expect(style.flexDirection).toBe('column')
    expect(style.rowGap).toBe('12px')
    expect(style.paddingTop).toBe('16px')
    expect(style.paddingLeft).toBe('16px')
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
    expect(style.borderRadius).toBe(getComputedStyle(probeOf(canvasElement, 'corner')).borderRadius)
    expect(style.borderTopWidth).toBe('1px')
  },
}
