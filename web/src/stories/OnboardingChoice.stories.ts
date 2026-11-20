import { defineComponent, h, ref } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingChoice from '../components/onboarding/OnboardingChoice.vue'
import OnboardingChoiceGroup from '../components/onboarding/OnboardingChoiceGroup.vue'

/**
 * One radio row of an onboarding group (the
 * `components-onboarding-onboarding-choice` inventory row, #3308). Props only:
 * the choice always renders inside a real OnboardingChoiceGroup, because the
 * owner requires the group context (it throws without it), so every story's
 * host is that production group. The choice is still the component under
 * scrutiny: the group only supplies label, name, model and disabled state.
 * Synthetic labels only.
 */
const meta = {
  id: 'onboarding-choice',
  title: 'Onboarding primitives/OnboardingChoice',
  component: OnboardingChoice,
  args: { value: 'storybook-alpha' },
} satisfies Meta<typeof OnboardingChoice>
export default meta
type Story = StoryObj<typeof meta>

interface ChoiceFixture {
  value: string
  title: string
  description?: string
  disabled?: boolean
}

/** A real group hosting production choices in the default list layout. */
function group(choices: ChoiceFixture[], modelValue = '', groupDisabled = false) {
  return defineComponent({
    name: 'OnboardingChoiceGroupStoryHost',
    setup() {
      const value = ref(modelValue)
      return () => h(OnboardingChoiceGroup, {
        label: 'Story group',
        name: 'storybook-group',
        modelValue: value.value,
        disabled: groupDisabled,
        'onUpdate:modelValue': (next: string) => { value.value = next },
      }, {
        default: () => choices.map((choice) => h(OnboardingChoice, {
          value: choice.value,
          disabled: choice.disabled,
        }, {
          default: () => choice.title,
          ...(choice.description ? { description: () => choice.description } : {}),
        })),
      })
    },
  })
}

export const Default: Story = {
  // Unchecked, plain title: no description slot, so the description element
  // is absent (the owner's description v-if branch is off).
  render: () => h(group([{ value: 'storybook-alpha', title: 'Ask me first' }], '')),
  play: async ({ canvasElement }) => {
    const label = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-choice')
      expect(found).toBeTruthy()
      return found!
    })
    const input = label.querySelector<HTMLInputElement>('input[type="radio"]')!
    expect(input.checked).toBe(false)
    expect(label.getAttribute('data-checked')).toBe('false')
    expect(label.getAttribute('data-disabled')).toBe('false')
    expect(label.querySelector('.ob-choice-desc')).toBeNull()
  },
}

export const Checked: Story = {
  render: () => h(group([{ value: 'storybook-alpha', title: 'Ask me first' }], 'storybook-alpha')),
  play: async ({ canvasElement }) => {
    const label = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-choice')
      expect(found).toBeTruthy()
      return found!
    })
    const input = label.querySelector<HTMLInputElement>('input[type="radio"]')!
    expect(input.checked).toBe(true)
    expect(label.getAttribute('data-checked')).toBe('true')
    // Clicking the already-checked radio fires no change event; the owner's
    // repeat() still re-selects, so the group value cannot be toggled off.
    await userEvent.click(input)
    expect(label.getAttribute('data-checked')).toBe('true')
    expect(input.checked).toBe(true)
  },
}

export const Description: Story = {
  render: () => h(group([{
    value: 'storybook-alpha',
    title: 'Ask me first',
    description: 'Nothing is filed or skipped for you; every suggestion waits for review.',
  }], '')),
  play: async ({ canvasElement }) => {
    const text = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.ob-choice-desc')
      expect(found).toBeTruthy()
      return found!
    })
    expect(text.textContent).toContain('every suggestion waits for review')
  },
}

export const Select: Story = {
  // Controlled group: the host owns modelValue and applies the owner's emit.
  render: () => h(group([
    { value: 'storybook-alpha', title: 'Ask me first' },
    { value: 'storybook-beta', title: "File what's clear" },
  ], 'storybook-alpha')),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(2))
    const beta = canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')[1]!
    await userEvent.click(beta)
    // The group's value moved and the owner re-synced the whole native group,
    // so afterwards exactly one radio is checked.
    await waitFor(() => {
      const boxes = canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')
      expect(boxes[0]!.checked).toBe(false)
      expect(boxes[1]!.checked).toBe(true)
      expect(canvasElement.querySelector('.ob-choice[data-checked="true"] input')!.getAttribute('value')).toBe('storybook-beta')
    })
  },
}

export const Disabled: Story = {
  // The choice's own `disabled` prop; the group-level disabled (AccessStage
  // before a runtime exists) renders identically through group.disabled and
  // has its own story on the group row.
  render: () => h(group([
    { value: 'storybook-alpha', title: 'Ask me first', disabled: true },
    { value: 'storybook-beta', title: "File what's clear" },
  ], '')),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(2))
    const boxes = canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')
    // The disabled choice is actually blocked.
    expect(boxes[0]!.closest('.ob-choice')!.getAttribute('data-disabled')).toBe('true')
    expect(boxes[0]!).toBeDisabled()
    await userEvent.click(boxes[0]!)
    expect(canvasElement.querySelectorAll('.ob-choice input:checked')).toHaveLength(0)
    // The enabled sibling beside it still works.
    await userEvent.click(boxes[1]!)
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice input:checked')).toHaveLength(1))
  },
}

export const KeyboardFocus: Story = {
  // Native radio keyboard path: Tab reaches the group's one tab stop, and the
  // arrow keys walk the choices firing the owner's change handler.
  render: () => h(group([
    { value: 'storybook-alpha', title: 'Ask me first' },
    { value: 'storybook-beta', title: "File what's clear" },
  ], 'storybook-alpha')),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelectorAll('.ob-choice')).toHaveLength(2))
    const alpha = canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')[0]!
    await userEvent.tab()
    expect(document.activeElement).toBe(alpha)
    expect(alpha.matches(':focus-visible')).toBe(true)
    await userEvent.keyboard('{ArrowDown}')
    await waitFor(() => {
      const beta = canvasElement.querySelectorAll<HTMLInputElement>('.ob-choice input')[1]!
      expect(document.activeElement).toBe(beta)
      expect(beta.checked).toBe(true)
      expect(document.querySelector('.ob-choice[data-checked="true"] input')).toBe(beta)
    })
  },
}
