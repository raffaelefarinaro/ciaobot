// Storybook for the global field and form family: the production form classes
// from `web/src/styles/app.css` rendered on native elements, never a copy of
// the CSS. The stylesheet import is the owner link the inventory coverage
// guard checks, and token assertions read through inert probe elements.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-fields-forms',
  title: 'Global CSS families/Global fields and forms',
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

const formGrid = `<div class="form-grid">
  <div class="form-group"><label for="story-alpha">Storybook alpha</label><input id="story-alpha" type="text"></div>
  <div class="form-group"><label for="story-beta">Storybook beta</label><input id="story-beta" type="text"></div>
</div>`

const tabTo = async (target: HTMLElement, limit = 10): Promise<void> => {
  for (let step = 0; step < limit && document.activeElement !== target; step += 1) await userEvent.tab()
  expect(document.activeElement, 'keyboard focus reached the story control').toBe(target)
}

// The family baseline: an unclassed native input, carrying only the global
// `input` element rules (fill, 1px --border edge, --radius corners).
export const Default: Story = {
  render: () => ({
    template: [
      '<input type="text" placeholder="Storybook value" aria-label="Storybook field">',
      probe('edge', 'border: 1px solid var(--border)'),
      probe('corner', 'border-radius: var(--radius)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const input = canvas.getByRole('textbox', { name: 'Storybook field' })
    const style = getComputedStyle(input)
    const edge = getComputedStyle(probeOf(canvasElement, 'edge'))
    expect(style.borderTopColor).toBe(edge.borderTopColor)
    expect(style.borderTopWidth).toBe(edge.borderTopWidth)
    expect(style.borderRadius).toBe(getComputedStyle(probeOf(canvasElement, 'corner')).borderRadius)
  },
}

// The two-column form grid at a wide canvas: both fields share the first row.
export const FormGrid: Story = {
  render: () => ({ template: formGrid }),
  play: async ({ canvasElement }) => {
    const grid = canvasElement.querySelector('.form-grid') as HTMLElement | null
    expect(grid, 'the form grid rendered').not.toBeNull()
    if (!grid) throw new Error('Missing form grid')
    const style = getComputedStyle(grid)
    expect(style.display).toBe('grid')
    expect(style.columnGap).toBe('12px')
    expect(style.rowGap).toBe('12px')
    const [first, second] = [...grid.querySelectorAll<HTMLInputElement>('.form-group input')]
    expect(Math.abs(second.offsetTop - first.offsetTop), 'two groups share the first grid row').toBeLessThanOrEqual(2)
  },
}

// The collapse under a 600px viewport: the media query follows the pinned
// 390px workshop viewport (the runner applies it to the page), so the two
// groups stack instead of sitting side by side.
export const Narrow: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => ({ template: formGrid }),
  play: async ({ canvasElement }) => {
    const grid = canvasElement.querySelector('.form-grid') as HTMLElement | null
    if (!grid) throw new Error('Missing form grid')
    expect(getComputedStyle(grid).display).toBe('grid')
    const [first, second] = [...grid.querySelectorAll<HTMLInputElement>('.form-group input')]
    expect(second.offsetTop, 'the second group stacks below the first').toBeGreaterThan(first.offsetTop)
  },
}

// The labelled group: a column of label over control with a 4px gap, and the
// label keeps its uppercase small-caps treatment.
export const FormGroup: Story = {
  render: () => ({
    template:
      '<div class="form-group"><label for="story-group">Storybook group label</label><input id="story-group" type="text" aria-label="Storybook group field"></div>',
  }),
  play: async ({ canvasElement }) => {
    const group = canvasElement.querySelector('.form-group')
    expect(group).not.toBeNull()
    const style = getComputedStyle(group!)
    expect(style.display).toBe('flex')
    expect(style.flexDirection).toBe('column')
    expect(style.rowGap).toBe('4px')
    const label = group!.querySelector('label')!
    expect(getComputedStyle(label).textTransform).toBe('uppercase')
  },
}

// The action row: a wrapping flex row, here carrying a primary and a small
// secondary exactly as forms lay them out.
export const FormActions: Story = {
  render: () => ({
    template:
      '<div class="form-actions"><button type="button" class="btn-primary">Storybook save</button><button type="button" class="btn-small">Storybook cancel</button></div>',
  }),
  play: async ({ canvas }) => {
    const actions = canvas.getByRole('button', { name: 'Storybook save' }).closest('.form-actions') as HTMLElement
    const style = getComputedStyle(actions)
    expect(style.display).toBe('flex')
    expect(style.flexWrap).toBe('wrap')
    expect(canvas.getByRole('button', { name: 'Storybook cancel' })).toBeTruthy()
  },
}

// A muted secondary hint.
export const Hint: Story = {
  render: () => ({
    template: [
      '<p class="hint">Storybook hint text</p>',
      probe('label', 'color: var(--fg2)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const hint = canvas.getByText('Storybook hint text')
    expect(getComputedStyle(hint).color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(getComputedStyle(hint).marginTop, 'the hint carries no extra margin around text').toBe('0px')
  },
}

// The warning hint: a block-level banded callout with an opaque tinted fill
// and a warning-edged border resolved through color-mix.
export const HintWarn: Story = {
  render: () => ({ template: '<p class="hint hint--warn">Storybook warning hint</p>' }),
  play: async ({ canvas }) => {
    const hint = canvas.getByText('Storybook warning hint')
    const style = getComputedStyle(hint)
    expect(hint.classList.contains('hint--warn')).toBe(true)
    expect(style.display).toBe('block')
    expect(style.backgroundColor, 'the warning tint paints an opaque fill').not.toBe('rgba(0, 0, 0, 0)')
    expect(style.borderTopColor, 'the tinted edge paints a visible colour').not.toBe('rgba(0, 0, 0, 0)')
  },
}

// The multi-select pill, rendered as production does (SchedulePanel): a label
// wrapping a hidden checkbox, so clicking the pill toggles that checkbox.
export const CheckboxPill: Story = {
  render: () => ({
    template: [
      '<label class="checkbox-pill"><input type="checkbox" hidden>Storyday</label>',
      probe('edge', 'border-color: var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const pill = canvas.getByText('Storyday')
    const style = getComputedStyle(pill)
    expect(style.height).toBe('30px')
    expect(style.minWidth).toBe('40px')
    expect(style.textTransform).toBe('uppercase')
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
    const checkbox = canvasElement.querySelector<HTMLInputElement>('.checkbox-pill input')
    expect(checkbox, 'the hidden checkbox renders').not.toBeNull()
    await userEvent.click(pill)
    expect(checkbox!.checked, 'clicking the pill toggles its checkbox').toBe(true)
  },
}

// The checked pill: .active paints the --accent2 fill under a --fg label.
export const CheckboxPillActive: Story = {
  render: () => ({
    template: [
      '<label class="checkbox-pill active"><input type="checkbox" hidden checked>Storyday</label>',
      probe('fill', 'background: var(--accent2)'),
      probe('label', 'color: var(--fg)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const pill = canvas.getByText('Storyday')
    expect(pill.classList.contains('active')).toBe(true)
    const style = getComputedStyle(pill)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// Keyboard focus on a field: the real Tab moves focus, the accent edge and the
// two-pixel focus ring settle over the token transitions, and focus is visible.
export const KeyboardFocus: Story = {
  render: () => ({
    template: [
      '<input type="text" aria-label="Storybook focus field">',
      probe('edge', 'color: var(--accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const input = canvas.getByRole('textbox', { name: 'Storybook focus field' })
    await tabTo(input)
    const accent = getComputedStyle(probeOf(canvasElement, 'edge')).color
    await waitFor(() =>
      expect(getComputedStyle(input).borderTopColor, 'the focused edge settles on the accent token').toBe(accent),
    )
    expect(getComputedStyle(input).boxShadow, 'the focus ring paints').not.toBe('none')
  },
}
