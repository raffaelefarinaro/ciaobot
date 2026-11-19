// Storybook for the global modal family: the production `.modal-backdrop` and
// `.modal-sheet` classes from `web/src/styles/app.css` rendered on native
// elements, never a copy of the CSS. The stylesheet import is the owner link
// the inventory coverage guard checks, and token assertions read through
// inert probe elements.
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect } from 'storybook/test'
import '../styles/app.css'

const meta = {
  id: 'global-modals',
  title: 'Global CSS families/Global modals',
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

const openModal = (body: string): string =>
  `<div class="modal-backdrop"><div class="modal-sheet"><h3>${body}</h3><p>Storybook sheet body</p></div></div>`

// The family default: an open modal — the dimming backdrop centring a sheet.
// The backdrop is position: fixed inset 0, so the story renders like the
// production overlay and the dim colour and z-index are asserted directly.
export const Default: Story = {
  render: () => ({ template: openModal('Storybook sheet title') }),
  play: async ({ canvas }) => {
    const backdrop = canvas.getByText('Storybook sheet title').closest('.modal-backdrop') as HTMLElement
    const style = getComputedStyle(backdrop)
    expect(style.position).toBe('fixed')
    expect(style.top).toBe('0px')
    expect(style.backgroundColor).toBe('rgba(0, 0, 0, 0.55)')
    expect(style.zIndex).toBe('100')
    expect(canvas.getByText('Storybook sheet body')).toBeTruthy()
  },
}

// The backdrop layer: fixed and full-frame, centring its content under a
// 16px padding inset.
export const ModalBackdrop: Story = {
  render: () => ({ template: openModal('Storybook backdrop title') }),
  play: async ({ canvas }) => {
    const backdrop = canvas.getByText('Storybook backdrop title').closest('.modal-backdrop') as HTMLElement
    const style = getComputedStyle(backdrop)
    expect(style.display).toBe('flex')
    expect(style.alignItems).toBe('center')
    expect(style.justifyContent).toBe('center')
    expect(style.paddingLeft).toBe('16px')
  },
}

// The sheet contract at a wide viewport: --radius-lg corners, a 1px edge,
// up to 520px, scrollable, a flex column.
export const ModalSheet: Story = {
  render: () => ({
    template: [
      openModal('Storybook sheet contract'),
      probe('corner', 'border-radius: var(--radius-lg)'),
      probe('edge', 'border-color: var(--border)'),
    ].join(''),
  }),
  play: async ({ canvas, canvasElement }) => {
    const sheet = canvas.getByText('Storybook sheet contract').closest('.modal-sheet') as HTMLElement
    const style = getComputedStyle(sheet)
    expect(style.display).toBe('flex')
    expect(style.flexDirection).toBe('column')
    expect(style.maxWidth).toBe('520px')
    expect(style.overflowY).toBe('auto')
    expect(style.borderRadius).toBe(getComputedStyle(probeOf(canvasElement, 'corner')).borderRadius)
    expect(style.borderTopColor).toBe(getComputedStyle(probeOf(canvasElement, 'edge')).borderTopColor)
    expect(style.borderTopWidth).toBe('1px')
  },
}

// The phone sheet: under a 600px viewport (pinned to the workshop's narrow
// 390px one, the media query follows the viewport, not a wrapper) the backdrop
// loses its inset and the sheet stretches frame to frame without corners.
export const Narrow: Story = {
  globals: { viewport: { value: 'narrow' } },
  render: () => ({ template: openModal('Storybook phone sheet') }),
  play: async ({ canvas }) => {
    const backdrop = canvas.getByText('Storybook phone sheet').closest('.modal-backdrop') as HTMLElement
    const sheet = canvas.getByText('Storybook phone sheet').closest('.modal-sheet') as HTMLElement
    expect(getComputedStyle(backdrop).paddingTop).toBe('0px')
    expect(getComputedStyle(backdrop).alignItems).toBe('stretch')
    expect(getComputedStyle(sheet).borderRadius).toBe('0px')
    expect(getComputedStyle(sheet).borderTopStyle).toBe('none')
    expect(getComputedStyle(sheet).maxWidth).toBe('none')
  },
}
