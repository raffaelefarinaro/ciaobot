import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { h, ref } from 'vue'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingButton from '../components/onboarding/OnboardingButton.vue'

/**
 * The one setup-screen button (the `components-onboarding-onboarding-button`
 * inventory row, #3308). Props only: variants are the component's own prop and
 * the disabled look is a fall-through attribute, so no fixture is declared.
 * Each story renders the production component.
 */
const meta = {
  id: 'onboarding-button',
  title: 'Onboarding primitives/OnboardingButton',
  component: OnboardingButton,
} satisfies Meta<typeof OnboardingButton>
export default meta
type Story = StoryObj<typeof meta>

export const Default: Story = {
  // The default variant is secondary: no props, slot text only.
  render: () => h(OnboardingButton, null, () => 'Continue'),
  play: async ({ canvasElement }) => {
    const button = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button')
      expect(found).toBeTruthy()
      return found!
    })
    expect(button.getAttribute('data-variant')).toBe('secondary')
    expect(button.getAttribute('type')).toBe('button')
    // 44px touch target: the component's own min-height contract.
    expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
  },
}

export const Primary: Story = {
  render: () => h(OnboardingButton, { variant: 'primary' }, () => 'Continue'),
  play: async ({ canvasElement }) => {
    const button = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button')
      expect(found).toBeTruthy()
      return found!
    })
    expect(button.getAttribute('data-variant')).toBe('primary')
  },
}

export const Link: Story = {
  render: () => h(OnboardingButton, { variant: 'link' }, () => 'Leave setup'),
  play: async ({ canvasElement }) => {
    const button = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button')
      expect(found).toBeTruthy()
      return found!
    })
    expect(button.getAttribute('data-variant')).toBe('link')
    expect(button.textContent).toBe('Leave setup')
  },
}

export const Disabled: Story = {
  // The disabled look is a fall-through attribute; production owners pass it
  // while a save is in flight (Shell's busy, RuntimeStage's loadingStatus).
  render: () => h(OnboardingButton, { disabled: true }, () => 'Continue'),
  play: async ({ canvasElement }) => {
    const button = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button')
      expect(found).toBeTruthy()
      return found!
    })
    // Actually blocked, not just styled.
    expect(button).toBeDisabled()
  },
}

export const KeyboardFocus: Story = {
  render: () => h(OnboardingButton, null, () => 'Continue'),
  play: async ({ canvasElement }) => {
    const button = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button')
      expect(found).toBeTruthy()
      return found!
    })
    await userEvent.tab()
    expect(document.activeElement).toBe(button)
    expect(button.matches(':focus-visible')).toBe(true)
    // Enter activates the native button: the owner's handler fires.
    const clicked = ref(0)
    canvasElement.querySelector('.ob-button')!.addEventListener('click', () => { clicked.value += 1 })
    await userEvent.keyboard('{Enter}')
    await waitFor(() => expect(clicked.value).toBe(1))
  },
}
