import { defineComponent, h, ref } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingChoiceGroup from '../components/onboarding/OnboardingChoiceGroup.vue'
import OnboardingChoice from '../components/onboarding/OnboardingChoice.vue'

/**
 * The group around the onboarding radios (the
 * `components-onboarding-onboarding-choice-group` inventory row, #3308).
 * Props only: `layout`, `disabled` and the `after` slot are the owner's own
 * prop variants, so no fixture is declared. The choices are production
 * OnboardingChoice rows; synthetic labels only.
 */
const meta = {
  id: 'onboarding-choice-group',
  title: 'Onboarding primitives/OnboardingChoiceGroup',
  component: OnboardingChoiceGroup,
  args: { label: 'Story group', name: 'storybook-group', modelValue: 'storybook-alpha' },
} satisfies Meta<typeof OnboardingChoiceGroup>
export default meta
type Story = StoryObj<typeof meta>

/**
 * A controlled group host: `modelValue` stays in the host's ref, so the
 * group's `select` emit is exercised on the rendered component.
 */
function groupHost(options: {
  layout?: 'list' | 'cards' | 'segmented'
  disabled?: boolean
  choices?: Array<{ value: string; title: string }>
  after?: string
}) {
  return defineComponent({
    name: 'OnboardingChoiceGroupStoryHost',
    setup() {
      const value = ref('storybook-alpha')
      return () => h(OnboardingChoiceGroup, {
        label: 'Story group',
        name: 'storybook-group',
        modelValue: value.value,
        disabled: options.disabled ?? false,
        layout: options.layout,
        'onUpdate:modelValue': (next: string) => { value.value = next },
      }, {
        default: () => (options.choices ?? [
          { value: 'storybook-alpha', title: 'Ask me first' },
          { value: 'storybook-beta', title: "File what's clear" },
          { value: 'storybook-gamma', title: 'File everything' },
        ]).map((choice) => h(OnboardingChoice, { value: choice.value }, () => choice.title)),
        ...(options.after ? { after: () => h('p', { class: 'hint' }, options.after) } : {}),
      })
    },
  })
}

export const Default: Story = {
  // Default list layout with one choice already checked.
  render: () => h(groupHost({})),
  play: async ({ canvasElement }) => {
    const radios = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(radios.getAttribute('data-layout')).toBe('list')
    // The labelled group: aria-labelledby points at the label element.
    const labelId = radios.getAttribute('aria-labelledby')!
    expect(canvasElement.querySelector<HTMLElement>(`#${labelId}`)!.textContent).toBe('Story group')
    // The native radios share the owner's group name.
    for (const input of [...canvasElement.querySelectorAll<HTMLInputElement>('input[type="radio"]')]) {
      expect(input.name).toBe('storybook-group')
    }
    expect(canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input:checked')).toHaveLength(1)
    expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(3)
  },
}

export const Cards: Story = {
  render: () => h(groupHost({ layout: 'cards' })),
  play: async ({ canvasElement }) => {
    const radios = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(radios.getAttribute('data-layout')).toBe('cards')
  },
}

export const Segmented: Story = {
  // Production layout of the Approval mode and Care groups: one wrapping row.
  render: () => h(groupHost({ layout: 'segmented', choices: [{ value: 'storybook-alpha', title: 'Engine default' }, { value: 'storybook-beta', title: 'Auto' }] })),
  play: async ({ canvasElement }) => {
    const radios = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(radios.getAttribute('data-layout')).toBe('segmented')
    // Exactly one checked row reads as the filled segment (data-checked).
    const segmented = canvasElement.querySelector('.ob-choice[data-checked="true"]')!
    expect(segmented.querySelector<HTMLInputElement>('input')!.value).toBe('storybook-alpha')
  },
}

export const Disabled: Story = {
  // The group's disabled prop (AccessStage passes it until a runtime exists).
  render: () => h(groupHost({ disabled: true })),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(3))
    const boxes = [...canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')]
    // Actually blocked, not just styled.
    for (const input of boxes) {
      expect(input).toBeDisabled()
      expect(input.closest('.ob-choice')!.getAttribute('data-disabled')).toBe('true')
    }
    await userEvent.click(boxes[1]!)
    expect(canvasElement.querySelectorAll('.ob-choice input:checked')).toHaveLength(1)
    expect(canvasElement.querySelector<HTMLInputElement>('.ob-choice input:checked')!.value).toBe('storybook-alpha')
  },
}

export const AfterSlot: Story = {
  // Production use: guidance under the choices (AccessStage, MemoryStage,
  // RuntimeStage).
  render: () => h(groupHost({ after: 'Suggested. Saved when you continue.' })),
  play: async ({ canvasElement }) => {
    const radios = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')
      expect(found).toBeTruthy()
      return found!
    })
    const hint = radios.parentElement!.querySelector<HTMLElement>('.hint')
    expect(hint).toBeTruthy()
    expect(hint!.textContent).toBe('Suggested. Saved when you continue.')
    // The slot renders after the choices container.
    expect(hint!.previousElementSibling).toBe(radios)
  },
}

export const KeyboardFocus: Story = {
  // Native radio-group keyboard contract: one tab stop for the group, then
  // the arrow keys walk the radios, each move firing the owner's select.
  render: () => h(groupHost({})),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(3))
    const boxes = [...canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')]
    await userEvent.tab()
    expect(document.activeElement).toBe(boxes[0])
    await userEvent.keyboard('{ArrowDown}')
    await waitFor(() => {
      expect(document.activeElement).toBe(boxes[1])
      expect(boxes[1]!.checked).toBe(true)
      expect(boxes[0]!.checked).toBe(false)
    })
    await userEvent.keyboard('{ArrowDown}')
    await waitFor(() => {
      expect(document.activeElement).toBe(boxes[2])
      expect(boxes[2]!.checked).toBe(true)
    })
  },
}
