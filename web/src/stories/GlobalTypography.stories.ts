// Storybook for the global typography family: the production text helper
// classes from `web/src/styles/app.css` rendered on headings and spans, never
// a copy of the CSS. The stylesheet import is the owner link the inventory
// coverage guard checks, and token assertions read through inert probe
// elements.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-typography',
  title: 'Global CSS families/Global typography',
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

// The family baseline: an unclassed heading, the treatment the shared helpers
// replace: no mono face, no uppercase, no letter-spacing.
export const Default: Story = {
  render: () => ({
    template: [
      '<h3>Storybook heading</h3>',
      probe('mono', 'font-family: var(--font-mono)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const heading = canvas.getByRole('heading', { name: 'Storybook heading' })
    const style = getComputedStyle(heading)
    expect(style.textTransform).toBe('none')
    expect(style.letterSpacing).toBe('normal')
    expect(style.fontFamily, 'the baseline keeps the proportional face').not.toBe(
      getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily,
    )
  },
}

// The section title: mono, extra-small, --fg2, 0.5px spacing, weight 600.
export const SectionTitle: Story = {
  render: () => ({
    template: [
      '<h3 class="section-title">Storybook section</h3>',
      probe('mono', 'font-family: var(--font-mono)'),
      probe('label', 'color: var(--fg2)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const title = canvas.getByRole('heading', { name: 'Storybook section' })
    const style = getComputedStyle(title)
    expect(style.fontFamily).toBe(getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily)
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
    expect(style.letterSpacing).toBe('0.5px')
    expect(style.fontWeight).toBe('600')
  },
}

// The eyebrow label: the mono face at uppercase, for the small all-caps meta
// headings.
export const LabelEyebrow: Story = {
  render: () => ({
    template: [
      '<h3 class="label-eyebrow">Storybook eyebrow</h3>',
      probe('mono', 'font-family: var(--font-mono)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const label = canvas.getByRole('heading', { name: 'Storybook eyebrow' })
    const style = getComputedStyle(label)
    expect(style.fontFamily).toBe(getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily)
    expect(style.textTransform).toBe('uppercase')
    expect(style.letterSpacing).toBe('0.5px')
  },
}

// The subsection title: a proportional --fg2 heading at weight 500.
export const SubsectionTitle: Story = {
  render: () => ({
    template: [
      '<h3 class="subsection-title">Storybook subsection</h3>',
      probe('mono', 'font-family: var(--font-mono)'),
      probe('label', 'color: var(--fg2)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const sub = canvas.getByRole('heading', { name: 'Storybook subsection' })
    const style = getComputedStyle(sub)
    expect(style.fontWeight).toBe('500')
    expect(style.fontFamily, 'the subsection keeps the proportional face').not.toBe(
      getComputedStyle(probeOf(canvasElement, 'mono')).fontFamily,
    )
    expect(style.color).toBe(getComputedStyle(probeOf(canvasElement, 'label')).color)
  },
}

// The visually hidden helper: absolute 1px clipping that screen readers still
// reach (getByText finds it) but that renders no box.
export const SrOnly: Story = {
  render: () => ({ template: '<span class="sr-only">Storybook hidden</span>' }),
  play: async ({ canvas }) => {
    const hidden = canvas.getByText('Storybook hidden')
    const style = getComputedStyle(hidden)
    expect(style.position).toBe('absolute')
    expect(style.width).toBe('1px')
    expect(style.height).toBe('1px')
    expect(style.overflow).toBe('hidden')
    expect(style.whiteSpace).toBe('nowrap')
    expect(style.clip, 'the box is clipped to nothing').toBe('rect(0px, 0px, 0px, 0px)')
    expect(hidden.textContent).toBe('Storybook hidden')
  },
}
