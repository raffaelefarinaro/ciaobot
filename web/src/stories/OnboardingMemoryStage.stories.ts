import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import OnboardingMemoryStage from '../components/onboarding/OnboardingMemoryStage.vue'
import type { OnboardingStageProps } from '../lib/onboarding'
import { handover, held, json, receipt, route, settled, stagePanel, workspacesBody, WORKSPACE } from './onboardingStagesFixture'

/**
 * OnboardingMemoryStage (the `components-onboarding-onboarding-memory-stage`
 * inventory row, #3309): the memory handover proposal. The stage reads its
 * preference and the onboarding receipt on mount through the project store's
 * composable, so it mounts with a fresh Pinia in the #3267 boundary and every
 * choice writes at once for the shell's Continue. The registry re-read a write
 * triggers is also declared, and a refused write rolls the group back.
 * Synthetic workspace names only.
 */
const meta = {
  id: 'onboarding-memory-stage',
  title: 'Onboarding stable stages/OnboardingMemoryStage',
  component: OnboardingMemoryStage,
  // The meta args exist for the CSF type only: each story's render mounts the
  // production stage with its own synthetic context.
  args: {
    context: { target: { kind: 'installation' }, workspace: WORKSPACE, revision: 2, provisioned: true },
    busy: false,
  },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof OnboardingMemoryStage>
export default meta
type Story = StoryObj<typeof meta>

const props = (): OnboardingStageProps => ({
  context: { target: { kind: 'installation' }, workspace: WORKSPACE, revision: 2, provisioned: true },
  busy: false,
})

const handoverRoute = (body: unknown, status = 200) =>
  route('GET', `/api/proposals/handover?workspace=${WORKSPACE}`, () => json(body, status))
const onboardingRoute = (status: 'in_progress' | 'completed') => route('GET', '/api/onboarding', () => json(receipt(status)))
const saveRoute = (respond: () => Response | Promise<never>) =>
  route('PATCH', `/api/workspaces/${WORKSPACE}`, respond)
/** A write moves the registry epoch, so the store re-reads /api/workspaces. */
const registryRoute = () => route('GET', '/api/workspaces', () => json(workspacesBody()))

export const Loading: Story = {
  // Both mount reads held: nothing has answered, so no suggestion may show.
  parameters: {
    fixture: {
      routes: [
        route('GET', `/api/proposals/handover?workspace=${WORKSPACE}`, held),
        route('GET', '/api/onboarding', held),
      ],
    },
  },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="off"]')!.checked).toBe(true)
    expect(canvasElement.querySelector('#memory-suggested')).toBeNull()
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
    // The placeholder matches the loaded geometry: the stored answer with no
    // hint and no skeleton (loading.detail records this, not a follow-up).
    expect(canvasElement.textContent).toContain('Ask me first')
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
  },
}

export const Default: Story = {
  // handover loaded with no eligible journey: Ask me first, nothing else.
  parameters: { fixture: { routes: [handoverRoute(handover()), onboardingRoute('completed')] } },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector<HTMLInputElement>('input[value="off"]')!.checked).toBe(true))
    expect(canvasElement.querySelector('#memory-suggested')).toBeNull()
    expect(canvasElement.textContent).not.toContain('The trusted service is unavailable')
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
    const choices = [...canvasElement.querySelectorAll<HTMLInputElement>('input[name="memory-handover"]')]
    expect(choices.map((input) => input.value)).toEqual(['off', 'dismiss', 'all'])
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const Suggested: Story = {
  // An in-progress journey may be offered the draft: 'all' proposed, unwritten.
  parameters: { fixture: { routes: [handoverRoute(handover()), onboardingRoute('in_progress')] } },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector<HTMLInputElement>('input[value="all"]')!.checked).toBe(true))
    const hint = canvasElement.querySelector('#memory-suggested')!
    expect(hint.textContent).toBe('Suggested. Saved when you continue.')
    // The suggestion is a draft: the choice is not written until it is tapped.
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const ServiceUnavailable: Story = {
  // acting_available false with a non-off draft: the notice renders, the
  // enforcement wording unchanged.
  parameters: { fixture: { routes: [handoverRoute(handover('off', false)), onboardingRoute('in_progress')] } },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('The trusted service is unavailable, so acceptance is not acting now.'))
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="all"]')!.checked).toBe(true)
    expect(canvasElement.querySelector('#memory-suggested')?.textContent).toBe('Suggested. Saved when you continue.')
    // The notice is a role=status line, not an alert and not a disabled control.
    expect(canvasElement.querySelector('[role="alert"]')).toBeNull()
    const notice = [...canvasElement.querySelectorAll('[role="status"]')].find((el) => el.textContent?.includes('The trusted service is unavailable'))
    expect(notice).toBeTruthy()
  },
}

export const LoadFailure: Story = {
  // The handover read fails: the alert reports the engine's message, Continue
  // stays blocked.
  parameters: { fixture: { routes: [handoverRoute({ error: 'Synthetic refusal' }, 503), onboardingRoute('completed')] } },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Synthetic refusal'))
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="off"]')!.checked).toBe(true)
    expect(canvasElement.querySelector('#memory-suggested')).toBeNull()
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":false,"error":"Synthetic refusal"}.')
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
  },
}

export const ChoiceSaved: Story = {
  // Choosing an answer writes it at once: read, receipt, write, registry re-read.
  parameters: {
    fixture: {
      routes: [
        handoverRoute(handover()),
        onboardingRoute('completed'),
        saveRoute(() => json(workspacesBody())),
        registryRoute(),
      ],
    },
  },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const dismiss = canvasElement.querySelector<HTMLInputElement>('input[value="dismiss"]')!
    await userEvent.click(dismiss)
    await waitFor(() => expect(dismiss.checked).toBe(true))
    await waitFor(() => expect(canvasElement.textContent).toContain('Fixture requests: 4.'))
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="off"]')!.checked).toBe(false)
    expect(canvasElement.querySelector('#memory-suggested')).toBeNull()
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const SaveRefused: Story = {
  // A refused write rolls the group back to the stored answer and shows the
  // error; tapping the option again becomes the retry.
  parameters: {
    fixture: {
      routes: [
        handoverRoute(handover()),
        onboardingRoute('completed'),
        saveRoute(() => json({ error: 'Synthetic refusal' }, 503)),
        registryRoute(),
      ],
    },
  },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props() }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const dismiss = canvasElement.querySelector<HTMLInputElement>('input[value="dismiss"]')!
    await userEvent.click(dismiss)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Synthetic refusal'))
    await waitFor(() => expect(dismiss.checked).toBe(false))
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="off"]')!.checked).toBe(true)
    expect(canvasElement.textContent).toContain('Fixture requests: 4.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":false,"error":"Synthetic refusal"}.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const Narrow: Story = {
  // The suggested render in the 390px container: choices and hint fit one column.
  parameters: { fixture: { routes: [handoverRoute(handover()), onboardingRoute('in_progress')] } },
  render: () => stagePanel({ label: 'memory', component: OnboardingMemoryStage, props: props(), narrow: true }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    const group = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')
      expect(found).toBeTruthy()
      return found!
    })
    expect(group.getBoundingClientRect().width).toBeLessThanOrEqual(391)
    expect(canvasElement.querySelector<HTMLInputElement>('input[value="all"]')!.checked).toBe(true)
    expect(canvasElement.querySelector('#memory-suggested')?.textContent).toBe('Suggested. Saved when you continue.')
    expect(canvasElement.textContent).toContain('Nothing is filed or skipped for you')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}
