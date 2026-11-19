// Storybook for the global badge family: the production badge classes from
// `web/src/styles/app.css` rendered on spans, never a copy of the CSS. The
// stylesheet import is the owner link the inventory coverage guard checks,
// and token assertions read through inert probe elements.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-badges',
  title: 'Global CSS families/Global badges',
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

const badge = (classes: string, text: string): string =>
  `<span class="${classes}">${text}</span>`

// The family baseline: an unclassed span, the before that the badge classes
// turn into a mono pill.
export const Default: Story = {
  render: () => ({
    template: [
      '<span>Storybook plain</span>',
      probe('mono', 'font-family: var(--font-mono)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const plain = canvas.getByText('Storybook plain')
    const style = getComputedStyle(plain)
    expect(style.fontFamily, 'the baseline keeps the proportional face, not the mono one').not.toBe(
      getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily,
    )
    expect(style.backgroundColor).toBe('rgba(0, 0, 0, 0)')
  },
}

// The badge base: mono, pill radius, nowrap, 2px/8px padding.
export const Badge: Story = {
  render: () => ({
    template: [
      badge('badge', 'Storybook badge'),
      probe('mono', 'font-family: var(--font-mono)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook badge')
    const style = getComputedStyle(badgeEl)
    expect(style.fontFamily).toBe(getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily)
    expect(style.borderRadius).toBe('10px')
    expect(style.whiteSpace).toBe('nowrap')
    expect(style.paddingTop).toBe('2px')
    expect(style.paddingLeft).toBe('8px')
  },
}

// The accent badge: accent fill, --on-accent label.
export const BadgeAccent: Story = {
  render: () => ({
    template: [
      badge('badge badge--accent', 'Storybook accent'),
      probe('fill', 'background: var(--accent)'),
      probe('label', 'color: var(--on-accent)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook accent')
    expect(badgeEl.classList.contains('badge--accent')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The secondary-accent badge: --accent2 fill, --fg label.
export const BadgeAccent2: Story = {
  render: () => ({
    template: [
      badge('badge badge--accent2', 'Storybook accent2'),
      probe('fill', 'background: var(--accent2)'),
      probe('label', 'color: var(--fg)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook accent2')
    expect(badgeEl.classList.contains('badge--accent2')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The muted badge: --bg3 fill behind a --fg2 label and a --border edge.
export const BadgeMuted: Story = {
  render: () => ({
    template: [
      badge('badge badge--muted', 'Storybook muted'),
      probe('fill', 'background: var(--bg3)'),
      probe('label', 'color: var(--fg2)'),
      probe('edge', 'border-color: var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook muted')
    expect(badgeEl.classList.contains('badge--muted')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
  },
}

// The builtin badge: a --accent2-tinted fill and edge over --bg3, both resolved
// through color-mix, so the play asserts that they paint visibly rather than
// restating the mix expression.
export const BadgeBuiltin: Story = {
  render: () => ({ template: badge('badge badge--builtin', 'Storybook builtin') }),
  play: async ({ canvas }) => {
    const badgeEl = canvas.getByText('Storybook builtin')
    expect(badgeEl.classList.contains('badge--builtin')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor, 'the tinted fill paints').not.toBe('rgba(0, 0, 0, 0)')
    expect(style.borderTopColor, 'the tinted edge paints').not.toBe('rgba(0, 0, 0, 0)')
  },
}

// The builtin badge under the light theme: `.theme-light .badge--builtin`
// swaps to the light-scope tokens. Production mounts that class on <html>,
// the selector matches any ancestor, and the rule's own fallbacks carry the
// light values it reads, so a themed wrapper reproduces the branch exactly.
export const BadgeBuiltinLight: Story = {
  render: () => ({
    template: `<div class="theme-light">${badge('badge badge--builtin', 'Storybook light builtin')}</div>`,
  }),
  play: async ({ canvas }) => {
    const badgeEl = canvas.getByText('Storybook light builtin')
    expect(badgeEl.closest('.theme-light'), 'the themed ancestor provides the branch').not.toBeNull()
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor, 'the light tint paints').not.toBe('rgba(0, 0, 0, 0)')
    expect(style.borderTopColor, 'the light edge paints').not.toBe('rgba(0, 0, 0, 0)')
  },
}

// The success badge: a translucent green fill with a --success label.
export const BadgeSuccess: Story = {
  render: () => ({
    template: [
      badge('badge badge--success', 'Storybook success'),
      probe('label', 'color: var(--success)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook success')
    expect(badgeEl.classList.contains('badge--success')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe('rgba(76, 175, 80, 0.15)')
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The warning badge: a translucent amber fill with a --warning label.
export const BadgeWarn: Story = {
  render: () => ({
    template: [
      badge('badge badge--warn', 'Storybook warn'),
      probe('label', 'color: var(--warning)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook warn')
    expect(badgeEl.classList.contains('badge--warn')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe('rgba(255, 152, 0, 0.15)')
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The error badge: a translucent red fill with an --error label.
export const BadgeError: Story = {
  render: () => ({
    template: [
      badge('badge badge--error', 'Storybook error'),
      probe('label', 'color: var(--error)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storybook error')
    expect(badgeEl.classList.contains('badge--error')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.backgroundColor).toBe('rgba(244, 67, 54, 0.15)')
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The resting dot marker: a dim, uppercase chip with no fill.
export const BadgeDot: Story = {
  render: () => ({
    template: [
      badge('badge badge--dot', 'Storyday'),
      probe('label', 'color: var(--fg2)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storyday')
    expect(badgeEl.classList.contains('badge--dot')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.opacity).toBe('0.35')
    expect(style.textTransform).toBe('uppercase')
    expect(style.backgroundColor).toBe('rgba(0, 0, 0, 0)')
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The active dot marker: .badge--dot.active raises the marker to full opacity
// over an --accent2 fill and an --fg label.
export const BadgeDotActive: Story = {
  render: () => ({
    template: [
      badge('badge badge--dot active', 'Storyday'),
      probe('fill', 'background: var(--accent2)'),
      probe('label', 'color: var(--fg)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const badgeEl = canvas.getByText('Storyday')
    expect(badgeEl.classList.contains('active')).toBe(true)
    const style = getComputedStyle(badgeEl)
    expect(style.opacity).toBe('1')
    expect(style.backgroundColor).toBe(getComputedStyle(probeOf(canvasElement, 'fill')).backgroundColor)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}
