import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { h } from 'vue'
import { expect, waitFor } from 'storybook/test'
import OnboardingStepFooter from '../components/onboarding/OnboardingStepFooter.vue'
import OnboardingButton from '../components/onboarding/OnboardingButton.vue'

/**
 * The one footer of a setup screen (the
 * `components-onboarding-onboarding-step-footer` inventory row, #3308). Props
 * only: the footer's content is its `start` and default slots, filled here
 * with the production buttons their one consumer (OnboardingShell) puts in
 * them. Synthetic labels only.
 */
const meta = {
  id: 'onboarding-step-footer',
  title: 'Onboarding primitives/OnboardingStepFooter',
  component: OnboardingStepFooter,
} satisfies Meta<typeof OnboardingStepFooter>
export default meta
type Story = StoryObj<typeof meta>

/** The Shell's footer slots: Back on the start, Leave setup and Continue on the end. */
function shellSlots(onLeave: () => void, onContinue: () => void) {
  return {
    start: () => h(OnboardingButton, { onClick: () => {}, class: 'back' }, () => 'Back'),
    default: () => [
      h(OnboardingButton, { variant: 'link', onClick: onLeave, class: 'leave' }, () => 'Leave setup'),
      h(OnboardingButton, { variant: 'primary', onClick: onContinue, class: 'continue' }, () => 'Continue'),
    ],
  }
}

export const Default: Story = {
  render: () => h(OnboardingStepFooter, null, shellSlots(() => {}, () => {})),
  play: async ({ canvasElement }) => {
    const footer = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('footer.ob-footer')
      expect(found).toBeTruthy()
      return found!
    })
    // Two rows: the start slot row, then the default slot row.
    const rows = footer.querySelectorAll('.ob-footer-row')
    expect(rows).toHaveLength(2)
    expect(rows[0]!.querySelector<HTMLButtonElement>('.ob-button.back')!.textContent).toBe('Back')
    expect(rows[1]!.querySelector<HTMLButtonElement>('.ob-button.leave')!.textContent).toBe('Leave setup')
    expect(rows[1]!.querySelector<HTMLButtonElement>('.ob-button.continue')!.textContent).toBe('Continue')
    // 44px touch targets for the footer's buttons.
    for (const button of [...footer.querySelectorAll<HTMLButtonElement>('.ob-button')]) {
      expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
  },
}

export const Narrow: Story = {
  // The 390px phone viewport: the owner's <=560px media query applies and the
  // footer rows stop wrapping.
  parameters: { viewport: { defaultViewport: 'narrow' } },
  render: () => h(OnboardingStepFooter, null, shellSlots(() => {}, () => {})),
  play: async ({ canvasElement }) => {
    const footer = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('footer.ob-footer')
      expect(found).toBeTruthy()
      return found!
    })
    // One row on a phone: tighter, non-wrapping rows, still no overflow, with
    // the same 44px targets.
    expect(getComputedStyle(footer).flexWrap).toBe('nowrap')
    for (const row of [...footer.querySelectorAll('.ob-footer-row')]) {
      expect(getComputedStyle(row as HTMLElement).flexWrap).toBe('nowrap')
    }
    expect(footer.scrollWidth).toBeLessThanOrEqual(footer.clientWidth + 1)
    for (const button of [...footer.querySelectorAll<HTMLButtonElement>('.ob-button')]) {
      expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    }
  },
}
