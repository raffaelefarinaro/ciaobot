// Storybook for the global focus family: the production `:focus-visible`
// outline rule from `web/src/styles/app.css`, proven on a real keyboard Tab.
// The stylesheet import is the owner link the inventory coverage guard checks,
// and the ring colour is read through an inert probe element.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-focus',
  title: 'Global CSS families/Global focus',
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

const tabTo = async (target: HTMLElement, limit = 10): Promise<void> => {
  for (let step = 0; step < limit && document.activeElement !== target; step += 1) await userEvent.tab()
  expect(document.activeElement, 'keyboard focus reached the story control').toBe(target)
}

// The resting control: no focus, no outline, the family's before-state.
export const Default: Story = {
  render: () => ({
    template: [
      '<button type="button">Storybook target</button>',
      '<button type="button">Storybook neighbour</button>',
    ].join(''),
  }),
  play: async ({ canvas }) => {
    const target = canvas.getByRole('button', { name: 'Storybook target' })
    expect(getComputedStyle(target).outlineStyle).toBe('none')
    expect(target.matches(':focus-visible')).toBe(false)
  },
}

// The focus ring: a real Tab lands on the second control, :focus-visible
// paints the 2px --accent outline, and focus is the intended element.
export const FocusVisible: Story = {
  render: () => ({
    template: [
      '<button type="button">Storybook neighbour</button>',
      '<button type="button">Storybook target</button>',
      probe('ring', 'color: var(--accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const target = canvas.getByRole('button', { name: 'Storybook target' })
    await tabTo(target)
    expect(target.matches(':focus-visible'), 'keyboard focus matches the rule').toBe(true)
    const style = getComputedStyle(target)
    expect(style.outlineStyle).toBe('solid')
    expect(style.outlineWidth).toBe('2px')
    expect(style.outlineColor).toBe(getComputedStyle(probeOf(canvasElement, 'ring')).color)
    expect(style.outlineOffset).toBe('2px')
  },
}
