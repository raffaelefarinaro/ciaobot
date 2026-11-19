import { defineComponent, h, ref } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingField from '../components/onboarding/OnboardingField.vue'

/**
 * The labelled control of the setup screens (the
 * `components-onboarding-onboarding-field` inventory row, #3308). Props only:
 * help/error are props, `below` is an implemented slot branch, so no fixture
 * is declared. The slotted control uses the scoped-slot contract the owner
 * passes its child (describedby and invalid), rendered on a real input.
 */
const meta = {
  id: 'onboarding-field',
  title: 'Onboarding primitives/OnboardingField',
  component: OnboardingField,
  args: { id: 'story-field', label: 'Workspace name' },
} satisfies Meta<typeof OnboardingField>
export default meta
type Story = StoryObj<typeof meta>

/**
 * Host that applies the scoped-slot payload to the slotted input, the way
 * production stages do (OnboardingFinishStage binds `aria-describedby` and
 * `aria-invalid` to what the field hands it).
 */
function fieldHost(options: { id: string; label: string; help?: string; error?: string; below?: string; helpSlot?: string }) {
  return defineComponent({
    name: 'OnboardingFieldStoryHost',
    setup() {
      const value = ref('')
      return () => h(OnboardingField, { id: options.id, label: options.label, help: options.help, error: options.error }, {
        default: ({ describedby, invalid }: { describedby: string; invalid: boolean }) => h('input', {
          type: 'text',
          id: options.id,
          'aria-describedby': describedby,
          'aria-invalid': invalid ? 'true' : 'false',
          value: value.value,
          'onInput': (event: InputEvent) => { value.value = (event.target as HTMLInputElement).value },
        }),
        ...(options.below ? { below: () => h('p', { class: 'hint' }, options.below) } : {}),
        ...(options.helpSlot ? { help: () => [h('p', 'The folder lives on this computer.'), h('p', [h('a', { href: '/settings' }, 'Open settings')])] } : {}),
      })
    },
  })
}

export const Default: Story = {
  // Label and control only: no help text, so the help element renders empty
  // (the owner's :empty rule hides it) even though the describedby chain
  // still names it.
  render: () => h(fieldHost({ id: 'story-field', label: 'Workspace name' })),
  play: async ({ canvasElement }) => {
    const root = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-field')
      expect(found).toBeTruthy()
      return found!
    })
    const label = root.querySelector<HTMLLabelElement>('label')!
    expect(label.getAttribute('for')).toBe('story-field')
    expect(label.textContent).toBe('Workspace name')
    const input = root.querySelector<HTMLInputElement>('input')!
    expect(input.getAttribute('aria-describedby')).toBe('story-field-help')
    expect(input.getAttribute('aria-invalid')).toBe('false')
    // No error paragraph in the clean render.
    expect(root.querySelector('.ob-field-error')).toBeNull()
    // The empty help area is the owner's own hidden-render contract.
    expect(root.querySelector('.ob-field-help')!.getAttribute('id')).toBe('story-field-help')
  },
}

export const Help: Story = {
  render: () => h(fieldHost({ id: 'story-field', label: 'Workspace name', help: 'You can add more workspaces later.' })),
  play: async ({ canvasElement }) => {
    const help = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-field-help')
      expect(found).toBeTruthy()
      return found!
    })
    expect(help.textContent).toBe('You can add more workspaces later.')
    const input = canvasElement.querySelector<HTMLInputElement>('.ob-field input')!
    expect(input.getAttribute('aria-describedby')).toBe('story-field-help')
  },
}

export const HelpSlot: Story = {
  // Production use of the help slot: OnboardingLocationStage's folder help is
  // rich markup (paragraphs and a link), not the plain help string.
  render: () => h(fieldHost({ id: 'story-field', label: 'Main Ciaobot folder', helpSlot: 'rich' })),
  play: async ({ canvasElement }) => {
    const help = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-field-help')
      expect(found).toBeTruthy()
      return found!
    })
    // The slot content, not the help prop, fills the help area.
    expect(help.querySelectorAll('p')).toHaveLength(2)
    const link = help.querySelector<HTMLAnchorElement>('a')!
    expect(link.getAttribute('href')).toBe('/settings')
    expect(link.textContent).toBe('Open settings')
  },
}

export const Error: Story = {
  render: () => h(fieldHost({ id: 'story-field', label: 'Workspace name', help: 'You can add more workspaces later.', error: 'A workspace with that name already exists.' })),
  play: async ({ canvasElement }) => {
    const root = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-field')
      expect(found).toBeTruthy()
      return found!
    })
    const alert = root.querySelector<HTMLElement>('.ob-field-error')!
    expect(alert.getAttribute('id')).toBe('story-field-error')
    expect(alert.getAttribute('role')).toBe('alert')
    expect(alert.textContent).toBe('A workspace with that name already exists.')
    const input = root.querySelector<HTMLInputElement>('input')!
    // The describedby chain now names help and error, and the invalid flag
    // reaches the slotted control.
    expect(input.getAttribute('aria-describedby')).toBe('story-field-help story-field-error')
    expect(input.getAttribute('aria-invalid')).toBe('true')
  },
}

export const Below: Story = {
  // The implemented `below` branch. No production onboarding consumer passes
  // it today (stages put chips in their own stacks), so this story claims the
  // branch's own contract, like the SidebarRow host-control story.
  render: () => h(fieldHost({ id: 'story-field', label: 'Local time', below: 'Suggested: 09:30 storybook-time' })),
  play: async ({ canvasElement }) => {
    const root = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-field')
      expect(found).toBeTruthy()
      return found!
    })
    const below = root.querySelector<HTMLElement>('.ob-field-below')!
    expect(below.textContent).toBe('Suggested: 09:30 storybook-time')
    // The below row sits directly after the slotted control and before help.
    expect(below.previousElementSibling!.tagName).toBe('INPUT')
    expect(below.nextElementSibling!.className).toBe('ob-field-help')
  },
}

export const KeyboardFocus: Story = {
  // Tab moves into the slotted control and the owner's deep rule draws the
  // focus outline on the input.
  render: () => h(fieldHost({ id: 'story-field', label: 'Workspace name' })),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('.ob-field input')).toBeTruthy())
    await userEvent.tab()
    const input = canvasElement.querySelector<HTMLInputElement>('.ob-field input')!
    expect(document.activeElement).toBe(input)
    expect(input.matches(':focus-visible')).toBe(true)
  },
}
