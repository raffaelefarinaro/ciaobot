import { defineComponent, h, ref } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, userEvent, waitFor } from 'storybook/test'
import OnboardingShell from '../components/onboarding/OnboardingShell.vue'
import OnboardingStack from '../components/onboarding/OnboardingStack.vue'
import type { OnboardingStageContext, OnboardingStep, OnboardingTarget } from '../lib/onboarding'

/**
 * The frame of a setup screen (the `components-onboarding-onboarding-shell`
 * inventory row, #3308). Props only: context, step, busy, error, conflict and
 * canContinue are the owner's props; the stage slot holds synthetic content in
 * place of a real stage, which stays in the stage slices' own rows. All data
 * is synthetic.
 */
const meta = {
  id: 'onboarding-shell',
  title: 'Onboarding primitives/OnboardingShell',
  component: OnboardingShell,
  args: {
    context: { target: { kind: 'installation' }, workspace: 'storybook-alpha', revision: 0, provisioned: true },
    step: 'location',
    busy: false,
    error: null,
    conflict: false,
    canContinue: true,
  },
} satisfies Meta<typeof OnboardingShell>
export default meta
type Story = StoryObj<typeof meta>

const ORDER: OnboardingStep[] = ['location', 'runtime', 'access_memory', 'care', 'finish']

const contextOf = (target: OnboardingTarget): OnboardingStageContext => ({
  target,
  workspace: 'storybook-alpha',
  revision: 0,
  provisioned: true,
})

/**
 * A controlled host: it applies the shell's emits the way OnboardingView does
 * (Back moves one step back, Reload is recorded on the host element), so the
 * shell's focus and button contracts run against real state.
 */
function shellHost(options: {
  target?: OnboardingTarget
  step?: OnboardingStep
  busy?: boolean
  error?: string | null
  conflict?: boolean
  canContinue?: boolean
  summary?: string
}) {
  return defineComponent({
    name: 'OnboardingShellStoryHost',
    setup() {
      const step = ref<OnboardingStep>(options.step ?? 'location')
      return () => h('div', { 'data-story-reloaded': 'false' }, [
        h(OnboardingShell, {
          context: contextOf(options.target ?? { kind: 'installation' }),
          step: step.value,
          busy: options.busy ?? false,
          error: options.error ?? null,
          conflict: options.conflict ?? false,
          canContinue: options.canContinue ?? true,
          onBack: () => {
            const index = ORDER.indexOf(step.value)
            if (index > 0) step.value = ORDER[index - 1]!
          },
          onContinue: () => {},
          onLeave: () => {},
          onReload: () => {
            const host = document.querySelector('[data-story-reloaded]')
            if (host) host.setAttribute('data-story-reloaded', 'true')
          },
        }, {
          stage: () => h(OnboardingStack, null, () => h('p', 'Synthetic stage content.')),
          ...(options.summary ? { summary: () => h('p', { class: 'hint' }, options.summary) } : {}),
        }),
      ])
    },
  })
}

export const Default: Story = {
  // An installation setup screen on the first step, ready to continue.
  render: () => h(shellHost({})),
  play: async ({ canvasElement }) => {
    const heading = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLHeadingElement>('#onboarding-title')
      expect(found).toBeTruthy()
      return found!
    })
    expect(heading.textContent).toBe('Your folder')
    expect(canvasElement.querySelector('.onboarding-context')!.textContent).toBe('This installation')
    // The steps nav marks the current step and nothing else.
    expect(canvasElement.querySelectorAll('.onboarding-steps li[aria-current="step"]')).toHaveLength(1)
    expect(canvasElement.querySelector('.onboarding-steps li[aria-current="step"]')!.textContent).toContain('Your folder')
    // The footer with its three controls, the primary enabled.
    expect(canvasElement.querySelector('.ob-button.back')!.textContent).toBe('Back')
    expect(canvasElement.querySelector('.ob-button.leave')!.textContent).toBe('Leave setup')
    const button = canvasElement.querySelector<HTMLButtonElement>('.ob-button.continue')!
    expect(button.textContent).toBe('Continue')
    expect(button).toBeEnabled()
    expect(button.getBoundingClientRect().height).toBeGreaterThanOrEqual(44)
    // No alert, no summary aside: the clean render.
    expect(canvasElement.querySelector('.onboarding-alert')).toBeNull()
    expect(canvasElement.querySelector('.onboarding-summary')).toBeNull()
  },
}

export const Workspace: Story = {
  // Production use: a workspace-targeted link opens the workspace journey.
  render: () => h(shellHost({ target: { kind: 'workspace', name: 'storybook-alpha' } })),
  play: async ({ canvasElement }) => {
    const context = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.onboarding-context')
      expect(found).toBeTruthy()
      return found!
    })
    expect(context.textContent).toBe('Workspace · storybook-alpha')
  },
}

export const Busy: Story = {
  // While a save or load is in flight every shell control is blocked.
  render: () => h(shellHost({ busy: true })),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('.ob-button.continue')).toBeTruthy())
    for (const name of ['.ob-button.back', '.ob-button.leave', '.ob-button.continue']) {
      expect(canvasElement.querySelector<HTMLButtonElement>(name)).toBeDisabled()
    }
  },
}

export const Blocked: Story = {
  // The stage is not ready (canContinue false), so only Continue is blocked;
  // Back and Leave stay usable, unlike the busy render.
  render: () => h(shellHost({ canContinue: false })),
  play: async ({ canvasElement }) => {
    await waitFor(() => expect(canvasElement.querySelector('.ob-button.continue')).toBeTruthy())
    expect(canvasElement.querySelector<HTMLButtonElement>('.ob-button.continue')).toBeDisabled()
    expect(canvasElement.querySelector<HTMLButtonElement>('.ob-button.back')).toBeEnabled()
    expect(canvasElement.querySelector<HTMLButtonElement>('.ob-button.leave')).toBeEnabled()
  },
}

export const Conflict: Story = {
  // Another device rewrote the receipt: the alert offers Reload.
  render: () => h(shellHost({ conflict: true })),
  play: async ({ canvasElement }) => {
    const alert = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.onboarding-alert')
      expect(found).toBeTruthy()
      return found!
    })
    expect(alert.getAttribute('role')).toBe('alert')
    expect(alert.textContent).toContain('Progress changed elsewhere. Reload to continue with the latest version.')
    const reload = alert.querySelector<HTMLButtonElement>('.ob-button')!
    expect(reload.textContent).toBe('Reload')
    expect(reload).toBeEnabled()
    await userEvent.click(reload)
    await waitFor(() => expect(canvasElement.querySelector('[data-story-reloaded]')!.getAttribute('data-story-reloaded')).toBe('true'))
  },
}

export const Error: Story = {
  // A stage save failed: the alert carries the message and one Reload.
  render: () => h(shellHost({ error: 'Could not reach the engine.' })),
  play: async ({ canvasElement }) => {
    const alert = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.onboarding-alert')
      expect(found).toBeTruthy()
      return found!
    })
    expect(alert.getAttribute('role')).toBe('alert')
    expect(alert.textContent).toContain('Could not reach the engine.')
    // The conflict paragraph is absent in this render.
    expect(alert.textContent).not.toContain('Progress changed elsewhere')
  },
}

export const ConflictError: Story = {
  // Both flags at once: production can pass a stage error while the engine
  // reports a conflict, so the alert shows both paragraphs over one Reload.
  render: () => h(shellHost({ conflict: true, error: 'Could not reach the engine.' })),
  play: async ({ canvasElement }) => {
    const alert = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.onboarding-alert')
      expect(found).toBeTruthy()
      return found!
    })
    expect(alert.querySelectorAll('p')).toHaveLength(2)
    expect(alert.textContent).toContain('Progress changed elsewhere')
    expect(alert.textContent).toContain('Could not reach the engine.')
    expect(alert.querySelectorAll('.ob-button')).toHaveLength(1)
  },
}

export const Finish: Story = {
  // The last step relabels the primary control and marks itself current.
  render: () => h(shellHost({ step: 'finish', canContinue: true })),
  play: async ({ canvasElement }) => {
    await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('.ob-button.continue')
      expect(found).toBeTruthy()
      return found!
    })
    expect(canvasElement.querySelector('.ob-button.continue')!.textContent).toBe('Finish setup')
    expect(canvasElement.querySelector('.onboarding-steps li[aria-current="step"]')!.textContent).toContain('Finish')
  },
}

export const SummarySlot: Story = {
  // The implemented summary aside branch. Its only owner (OnboardingView)
  // passes no summary slot today, so, like the SidebarRow host-control story,
  // this claims the branch's own contract rather than a live screen.
  render: () => h(shellHost({ summary: 'Synthetic setup summary content.' })),
  play: async ({ canvasElement }) => {
    const aside = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.onboarding-summary')
      expect(found).toBeTruthy()
      return found!
    })
    expect(aside.getAttribute('aria-label')).toBe('Setup summary')
    expect(aside.textContent).toContain('Synthetic setup summary content.')
  },
}

export const StepFocus: Story = {
  // Moving between steps focuses the heading (the screen-reader navigation
  // target). The h1 deliberately keeps its outline hidden (h1:focus has
  // outline: none), so only the activeElement move is asserted here, not a
  // visible focus ring. The Back button is the keyboard-reachable control.
  render: () => h(shellHost({ step: 'runtime' })),
  play: async ({ canvasElement }) => {
    const heading = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLHeadingElement>('#onboarding-title')
      expect(found).toBeTruthy()
      return found!
    })
    expect(heading.textContent).toBe('Your assistant')
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('.ob-button.back')!)
    // Back moved to the first step and the heading took announcement focus.
    await waitFor(() => {
      const moved = canvasElement.querySelector<HTMLHeadingElement>('#onboarding-title')!
      expect(document.activeElement).toBe(moved)
      expect(moved.textContent).toBe('Your folder')
    })
  },
}
