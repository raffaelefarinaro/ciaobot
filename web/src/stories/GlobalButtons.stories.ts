// Storybook for the global button families: the production shared button
// classes from `web/src/styles/app.css` rendered on native elements, never a
// copy of the CSS. The stylesheet import is the owner link the inventory
// coverage guard checks, and every token assertion reads the value through an
// inert probe element carrying the same custom property, so no story pins an
// absolute colour or pixel size.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-buttons',
  title: 'Global CSS families/Global buttons',
  tags: ['!autodocs'],
} satisfies Meta
export default meta
type Story = StoryObj<typeof meta>

/** An invisibly rendered element carrying one production token value: a computed
 *  read of a story element can then be compared against the token without the
 *  story restyling anything itself. */
const probe = (name: string, style: string): string =>
  `<span aria-hidden="true" data-probe="${name}" style="position: absolute; width: 0; height: 0; overflow: hidden; pointer-events: none; ${style}"></span>`

const probeOf = (canvasElement: HTMLElement, name: string): HTMLElement => {
  const found = canvasElement.querySelector(`[data-probe="${name}"]`)
  if (!found) throw new Error(`Missing probe: ${name}`)
  return found as HTMLElement
}

const touchProbe = probe('touch', 'height: var(--touch)')

/** Tabs until the given element holds focus: the number of stops before the
 *  story's own control is irrelevant, only that keyboard focus reached it. */
const tabTo = async (target: HTMLElement, limit = 10): Promise<void> => {
  for (let step = 0; step < limit && document.activeElement !== target; step += 1) await userEvent.tab()
  expect(document.activeElement, 'keyboard focus reached the story control').toBe(target)
}

// The family baseline: an unclassed native button, styled only by app.css's
// global `button` element rules plus the shared focus ring when reached by
// keyboard.
export const Default: Story = {
  render: () => ({ template: '<button type="button">Storybook action</button>' }),
  play: async ({ canvas }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook action' })
    expect(btn.classList.length, 'the baseline carries no class').toBe(0)
    expect(getComputedStyle(btn).touchAction).toBe('manipulation')
    await userEvent.click(btn)
    expect(document.activeElement).toBe(btn)
  },
}

// The anchor variant: accent fill with an --on-accent label and no border.
export const BtnPrimary: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-primary">Storybook primary</button>',
      probe('fill', 'background: var(--accent)'),
      probe('label', 'color: var(--on-accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook primary' })
    const style = getComputedStyle(btn)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.borderTopWidth).toBe('0px')
  },
}

// The small secondary variant: a 1px --border outline over the --bg3 fill.
export const BtnSmall: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-small">Storybook secondary</button>',
      probe('fill', 'background: var(--bg3)'),
      probe('edge', 'border: 1px solid var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook secondary' })
    const style = getComputedStyle(btn)
    const edge = getComputedStyle(probeOf(canvasElement, 'edge'))
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.borderTopColor).toBe(edge.borderTopColor)
    expect(style.borderTopWidth).toBe(edge.borderTopWidth)
    expect(style.borderTopStyle).toBe(edge.borderTopStyle)
  },
}

// The compact primary stacking: .btn-primary.btn-small keeps the primary fill
// and gains the 1px accent outline the plain primary never has.
export const PrimarySmall: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-primary btn-small">Storybook compact primary</button>',
      probe('fill', 'background: var(--accent)'),
      probe('edge', 'border-color: var(--accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook compact primary' })
    const style = getComputedStyle(btn)
    expect(btn.classList.contains('btn-primary')).toBe(true)
    expect(btn.classList.contains('btn-small')).toBe(true)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
    expect(style.borderTopStyle).toBe('solid')
  },
}

// The icon-only variant: transparent on a full --touch box, named for screen
// readers because it has no visible text.
export const BtnIcon: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-icon" aria-label="Storybook icon action"></button>',
      touchProbe,
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook icon action' })
    const box = btn.getBoundingClientRect()
    const touch = probeOf(canvasElement, 'touch').getBoundingClientRect().height
    expect(box.width, 'the icon box keeps the full --touch width').toBeGreaterThanOrEqual(touch - 0.5)
    expect(box.height, 'the icon box keeps the full --touch height').toBeGreaterThanOrEqual(touch - 0.5)
    expect(getComputedStyle(btn).borderTopColor).toBe('rgba(0, 0, 0, 0)')
  },
}

// The pressed icon: .btn-icon[aria-pressed="true"] paints the --bg3 fill over
// a --border edge, the aria state being the real signal.
export const BtnIconPressed: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-icon" aria-pressed="true" aria-label="Storybook pressed icon"></button>',
      probe('fill', 'background: var(--bg3)'),
      probe('edge', 'border-color: var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook pressed icon' })
    expect(btn.getAttribute('aria-pressed')).toBe('true')
    const style = getComputedStyle(btn)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
  },
}

// The ghost/tertiary chip: transparent fill, a 1px --border edge and a --fg2
// label, as "talk about it" and picker-row actions use it.
export const BtnChip: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-chip">Storybook chip</button>',
      probe('label', 'color: var(--fg2)'),
      probe('edge', 'border-color: var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook chip' })
    const style = getComputedStyle(btn)
    expect(style.backgroundColor).toBe('rgba(0, 0, 0, 0)')
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
  },
}

// The selected chip: .btn-chip.active turns the label and the edge accent.
export const BtnChipActive: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-chip active">Storybook selected chip</button>',
      probe('label', 'color: var(--accent)'),
      probe('edge', 'border-color: var(--accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook selected chip' })
    expect(btn.classList.contains('active')).toBe(true)
    const style = getComputedStyle(btn)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
  },
}

// The destructive modifier, stacked on a base exactly as production does
// (SettingsView renders `btn-small btn-danger` on its delete actions): it
// forces the edge and the label to --error without owning any fill of its own.
export const BtnDanger: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-small btn-danger">Storybook delete</button>',
      probe('label', 'color: var(--error)'),
      probe('edge', 'border-color: var(--error)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook delete' })
    expect(btn.classList.contains('btn-danger')).toBe(true)
    const style = getComputedStyle(btn)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
  },
}

// The blocked control: a disabled primary keeps its greyed fill and forbids
// both the pointer and the focus, not just the styling.
export const Disabled: Story = {
  render: () => ({
    template: '<button type="button" class="btn-primary" disabled>Storybook disabled</button>',
  }),
  play: async ({ canvas }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook disabled' }) as HTMLButtonElement
    expect(btn.disabled, 'the control is actually blocked').toBe(true)
    expect(getComputedStyle(btn).cursor).toBe('not-allowed')
    await userEvent.click(btn)
    expect(btn.matches(':focus')).toBe(false)
  },
}

// The touch-hit modifier: content-box sizing with the negative-margin trade so
// compact controls still present a full --touch target; the decorative ::before
// stays at the --touch-hit-visual inset. The icon keeps its own 8px rhythm over
// the modifier's calc padding (app.css declares .btn-icon after .touch-hit).
export const TouchHit: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-icon touch-hit" aria-label="Storybook touch target"></button>',
      touchProbe,
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook touch target' })
    const style = getComputedStyle(btn)
    expect(style.boxSizing).toBe('content-box')
    expect(getComputedStyle(btn, '::before').top).toBe('7px')
    const box = btn.getBoundingClientRect()
    const touch = probeOf(canvasElement, 'touch').getBoundingClientRect().height
    expect(box.width, 'the hit width stays within a half pixel of --touch').toBeGreaterThanOrEqual(touch - 0.5)
    expect(box.height, 'the hit height stays within a half pixel of --touch').toBeGreaterThanOrEqual(touch - 0.5)
  },
}

// The hover feedback: :hover applies only to the browser's real pointer
// position, and the workshop's synthetic user event API cannot move it (the
// Hover play below proved a dispatched hover never paints the rule). The play
// therefore asserts the shipped hover rule instead, the accepted pattern for
// runner-invisible states (SkeletonLoader's reduced-motion story). The anchor
// still renders underneath.
export const Hover: Story = {
  render: () => ({
    template: '<button type="button" class="btn-primary">Storybook hover</button>',
  }),
  play: async ({ canvas }) => {
    canvas.getByRole('button', { name: 'Storybook hover' })
    const styles = [...document.querySelectorAll('style')].map((element) => element.textContent ?? '')
    const owner = styles.find((text) => text.includes('.btn-primary:hover'))
    expect(owner, 'app.css is in the document styles').toBeDefined()
    expect(owner).toContain('.btn-primary:hover:not(:disabled)')
    expect(owner).toContain('background: var(--accent-strong)')
  },
}

// The pressed feedback: :active scales and repaints only during a real held
// pointer press, which no runner API can hold. The play asserts the shipped
// press rule instead, the same shipped-CSS pattern the SkeletonLoader
// reduced-motion story uses. The anchor still renders underneath.
export const ActivePress: Story = {
  render: () => ({
    template: '<button type="button" class="btn-primary">Storybook press</button>',
  }),
  play: async ({ canvas }) => {
    canvas.getByRole('button', { name: 'Storybook press' })
    const styles = [...document.querySelectorAll('style')].map((element) => element.textContent ?? '')
    const owner = styles.find((text) => text.includes('.btn-primary:active'))
    expect(owner, 'app.css is in the document styles').toBeDefined()
    expect(owner).toContain('transform: scale(0.98)')
  },
}

// Keyboard focus draws the shared :focus-visible ring, asserted on a real Tab.
export const KeyboardFocus: Story = {
  render: () => ({
    template: [
      '<button type="button" class="btn-primary">Storybook focus</button>',
      probe('ring', 'color: var(--accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const btn = canvas.getByRole('button', { name: 'Storybook focus' })
    await tabTo(btn)
    const style = getComputedStyle(btn)
    expect(style.outlineStyle).toBe('solid')
    expect(style.outlineWidth).toBe('2px')
    expect(style.outlineColor).toBe(getComputedStyle(probeOf(canvasElement, 'ring')).color)
    expect(style.outlineOffset).toBe('2px')
  },
}
