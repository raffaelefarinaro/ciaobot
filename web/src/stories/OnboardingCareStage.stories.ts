import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import OnboardingCareStage from '../components/onboarding/OnboardingCareStage.vue'
import type { OnboardingStageProps } from '../lib/onboarding'
import { careRoutine, held, json, route, settled, stagePanel, WORKSPACE } from './onboardingStagesFixture'

/**
 * OnboardingCareStage (the `components-onboarding-onboarding-care-stage`
 * inventory row, #3309): the Workspace care routine proposal. The stage reads
 * /api/schedules on mount and writes the edited row for the shell's Continue
 * through `store.updateSchedule`; both run inside the #3267 boundary. The
 * exposed `save` is driven by the host's `Call stage save` control, which is
 * the seam the shell binds, not an actor inside the stage. Synthetic routine
 * only.
 */
const meta = {
  id: 'onboarding-care-stage',
  title: 'Onboarding stable stages/OnboardingCareStage',
  component: OnboardingCareStage,
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
} satisfies Meta<typeof OnboardingCareStage>
export default meta
type Story = StoryObj<typeof meta>

const props = (): OnboardingStageProps => ({
  context: { target: { kind: 'installation' }, workspace: WORKSPACE, revision: 2, provisioned: true },
  busy: false,
})

const schedules = (respond: () => Response | Promise<never>) => route('GET', '/api/schedules', respond)
const patchRoutine = (respond: () => Response | Promise<never>) =>
  route('PATCH', `/api/schedules/system-memory-curation@${WORKSPACE}`, respond)

export const Loading: Story = {
  // The held schedule read: nothing has answered yet.
  parameters: { fixture: { routes: [schedules(held)] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No care routine is installed for this workspace.'))
    // The loading placeholder borrows the loaded empty copy: no availability
    // group and no row-shaped skeleton. Recorded for the loading follow-up.
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeNull()
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const NoRoutine: Story = {
  // A loaded list without the system-memory-curation routine: Continue passes.
  parameters: { fixture: { routes: [schedules(() => json([]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No care routine is installed for this workspace.'))
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeNull()
    const status = [...canvasElement.querySelectorAll('[role="status"]')].find((el) => el.textContent?.includes('No care routine is installed'))
    expect(status).toBeTruthy()
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
    expect(canvasElement.textContent).toContain('Fixture requests: 1.')
  },
}

export const LoadFailure: Story = {
  // A failed schedule read: the alert reports the engine's message.
  parameters: { fixture: { routes: [schedules(() => json({ error: 'Synthetic refusal' }, 503))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Synthetic refusal'))
    // The error hides the no-routine sentence: !routine && !error does not hold.
    expect(canvasElement.textContent).not.toContain('No care routine is installed')
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeNull()
    // Continue is never a dead end in the owner: the error rides the emitted
    // state while canContinue stays bound to a pending save only.
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":"Synthetic refusal"}.')
  },
}

// The read counter the refresh story's route answers from. The render resets it,
// so each mount starts with the first (loaded) read.
let careReads = 0

export const RefreshFailure: Story = {
  // The exposed refresh re-runs the read: a refused re-read keeps the routine on
  // screen and reports the engine's message beside it.
  parameters: {
    fixture: {
      routes: [
        schedules(() => (++careReads === 1 ? json([careRoutine()]) : json({ error: 'Synthetic refusal' }, 503))),
      ],
    },
  },
  render: () => {
    careReads = 0
    return stagePanel({ label: 'care', component: OnboardingCareStage, props: props(), exposed: ['refresh'] })
  },
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Workspace care is on.'))
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button.stage-host-button')!)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Synthetic refusal'))
    expect(canvasElement.textContent).toContain('Workspace care is on.')
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":"Synthetic refusal"}.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const Default: Story = {
  // A routine installed and enabled: availability, proposed slot and timezone.
  parameters: { fixture: { routes: [schedules(() => json([careRoutine()]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Workspace care is on.'))
    const group = canvasElement.querySelector<HTMLElement>('[role="radiogroup"]')!
    expect(group.getAttribute('data-layout')).toBe('segmented')
    const availability = [...canvasElement.querySelectorAll<HTMLInputElement>('input[name="availability"]')]
    expect(availability.map((input) => input.value)).toEqual(['always on', 'daytime', 'varies'])
    expect(availability[0]!.checked).toBe(true)
    const time = canvasElement.querySelector<HTMLInputElement>('input[type="time"]')!
    expect(time.value).toBe('08:00')
    const zone = canvasElement.querySelector<HTMLInputElement>('input[name="timezone"]')!
    expect(zone.value).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone)
    expect(canvasElement.textContent).toContain('The packaged default stays unless you change it. Nothing is written.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const Paused: Story = {
  // routine.enabled false: the head reads paused, the proposal stays editable.
  parameters: { fixture: { routes: [schedules(() => json([careRoutine({ enabled: false })]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Workspace care is paused.'))
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeTruthy()
    expect(canvasElement.textContent).not.toContain('Workspace care is on.')
    expect(canvasElement.textContent).toContain('The packaged default stays unless you change it. Nothing is written.')
  },
}

export const AvailabilityEdit: Story = {
  // An availability answer proposes its local slot until the time is edited.
  parameters: { fixture: { routes: [schedules(() => json([careRoutine()]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const varies = canvasElement.querySelector<HTMLInputElement>('input[value="varies"]')!
    await userEvent.click(varies)
    await waitFor(() => expect(varies.checked).toBe(true))
    // The checked segment marks the answer and the slot follows the suggestion.
    expect(canvasElement.querySelector('.ob-choice[data-checked="true"]')!.querySelector('input')!.value).toBe('varies')
    await waitFor(() => expect(canvasElement.querySelector<HTMLInputElement>('input[type="time"]')!.value).toBe('09:00'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Saved when you continue.'))
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const TimezoneEdit: Story = {
  // Typing into the timezone marks the proposal edited for the shell's Continue.
  parameters: { fixture: { routes: [schedules(() => json([careRoutine()]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props() }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const zone = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLInputElement>('input[name="timezone"]')
      expect(found).toBeTruthy()
      return found!
    })
    // Replace the proposal: typing appends, so the timezone is cleared first.
    await userEvent.clear(zone)
    await userEvent.type(zone, 'Europe/Paris')
    await waitFor(() => expect(canvasElement.textContent).toContain('Saved when you continue.'))
    expect(zone.value).toBe('Europe/Paris')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":true,"error":null}.')
  },
}

export const SaveApplied: Story = {
  // The shell's Continue drives the exposed save: the PATCH writes the edit.
  parameters: {
    fixture: {
      routes: [
        schedules(() => json([careRoutine()])),
        patchRoutine(() => json(careRoutine({ daily_time_utc: '09:00' }))),
      ],
    },
  },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props(), exposed: ['save'] }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const varies = canvasElement.querySelector<HTMLInputElement>('input[value="varies"]')!
    await userEvent.click(varies)
    await waitFor(() => expect(canvasElement.textContent).toContain('Saved when you continue.'))
    const save = canvasElement.querySelector<HTMLButtonElement>('button.stage-host-button')!
    expect(save.textContent?.trim()).toBe('Call stage save')
    await userEvent.click(save)
    // The write answers: the proposal is stored and the hint returns to the
    // packaged default, with no further write queued.
    await waitFor(() => expect(canvasElement.textContent).toContain('The packaged default stays unless you change it. Nothing is written.'))
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":true,"error":null}.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const SaveRefused: Story = {
  // A refused PATCH through the exposed save: the alert shows the engine's
  // message and the proposal stays edited for a retry.
  parameters: {
    fixture: {
      routes: [
        schedules(() => json([careRoutine()])),
        patchRoutine(() => json({ error: 'Synthetic refusal' }, 503)),
      ],
    },
  },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props(), exposed: ['save'] }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(canvasElement.querySelector<HTMLInputElement>('input[value="varies"]')!)
    await waitFor(() => expect(canvasElement.textContent).toContain('Saved when you continue.'))
    await userEvent.click(canvasElement.querySelector<HTMLButtonElement>('button.stage-host-button')!)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Synthetic refusal'))
    expect(canvasElement.textContent).toContain('Saved when you continue.')
    expect(canvasElement.textContent).toContain('Fixture requests: 2.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":true,"error":"Synthetic refusal"}.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const Narrow: Story = {
  // At 390px the time and timezone fields stack: the proposal stays one column.
  parameters: { fixture: { routes: [schedules(() => json([careRoutine()]))] } },
  render: () => stagePanel({ label: 'care', component: OnboardingCareStage, props: props(), narrow: true }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    const stage = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.care-stage')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stage.getBoundingClientRect().width).toBeLessThanOrEqual(391)
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeTruthy()
    const time = canvasElement.querySelector<HTMLInputElement>('.care-stage .fields input[type="time"]')!
    const zone = canvasElement.querySelector<HTMLInputElement>('.care-stage .fields input[name="timezone"]')!
    expect(zone.getBoundingClientRect().top - time.getBoundingClientRect().top).toBeGreaterThan(0)
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}
