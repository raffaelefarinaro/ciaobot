import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsMemoryHandover from '../components/settings/SettingsMemoryHandover.vue'
import type { HandoverAgreement, HandoverResponse } from '../lib/types'
import { firstThenFailing, held, json, ownerPanel, route, seedWorkspace, settled, WORKSPACE } from './settingsAccessFixture'

/**
 * SettingsMemoryHandover (the `components-settings-settings-memory-handover`
 * inventory row, #3307): the memory handover choice in Workspace overview. It
 * reads `/api/proposals/handover` for the active workspace and saves its one
 * field through the workspace PATCH, both through the #3267 boundary. The
 * synthetic workspace is seeded into the project store.
 */
const meta = {
  id: 'settings-memory-handover',
  title: 'Settings access and memory/SettingsMemoryHandover',
  component: SettingsMemoryHandover,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsMemoryHandover>
export default meta
type Story = StoryObj<typeof meta>

const NO_DECISIONS: HandoverAgreement = {
  decided: 0,
  matched: 0,
  rate: null,
  window: 30,
  min_decisions: 5,
  threshold: 0.8,
  predicted_accept: 0,
  accept_matched: 0,
  predicted_dismiss: 0,
  dismiss_matched: 0,
  suggest_handover: false,
}

const handover = (overrides: Partial<HandoverResponse> = {}): HandoverResponse => ({
  workspace: WORKSPACE,
  handover: 'off',
  memory_approval: {},
  accept_resolved: {
    memory: { mode: 'approve', inherited: true },
    profile: { mode: 'approve', inherited: true },
  },
  acting_available: true,
  agreement: NO_DECISIONS,
  ...overrides,
})

const withAgreement = (overrides: Partial<HandoverAgreement>): HandoverAgreement => ({ ...NO_DECISIONS, ...overrides })

const EVIDENCE = withAgreement({
  decided: 12,
  matched: 9,
  rate: 0.75,
  predicted_accept: 8,
  accept_matched: 7,
  predicted_dismiss: 4,
  dismiss_matched: 2,
})

const readRoute = (respond: () => Response | Promise<never>) =>
  route('GET', `/api/proposals/handover?workspace=${WORKSPACE}`, respond)
const saveRoute = (respond: () => Response | Promise<never>) =>
  route('PATCH', `/api/workspaces/${WORKSPACE}`, respond)
const savedWorkspaces = () => json({ workspaces: [{ name: WORKSPACE, vault_root: '/storybook/alpha', default_provider: 'claude' }], active: WORKSPACE })
// A saved or refused workspace write re-reads the registry, so both answers are declared.
const registryRoute = () => route('GET', '/api/workspaces', savedWorkspaces)

const panel = () => ownerPanel({ label: 'SettingsMemoryHandover', component: SettingsMemoryHandover, seed: seedWorkspace })

function radio(canvasElement: HTMLElement, label: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll('[role="radio"]')].find((item) => item.textContent?.trim() === label)
  if (!found) throw new Error(`No radio named ${label}`)
  return found as HTMLButtonElement
}

function buttonNamed(canvasElement: HTMLElement, name: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === name)
  if (!found) throw new Error(`No button named ${name}`)
  return found
}

export const Loading: Story = {
  parameters: { fixture: { routes: [readRoute(held)] } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Reading your decisions'))
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeNull()
  },
}

export const Failed: Story = {
  parameters: {
    fixture: { routes: [readRoute(() => json({ error: 'Synthetic handover refused' }, 500))] },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the handover status: Synthetic handover refused'))
    await userEvent.click(buttonNamed(canvasElement, 'Try again'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the handover status'))
    expect(canvasElement.querySelector('[role="radiogroup"]')).toBeNull()
  },
}

export const NoDecisions: Story = {
  parameters: {
    fixture: {
      routes: [
        readRoute(() => json(handover())),
        saveRoute(savedWorkspaces),
        registryRoute(),
      ],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('No decisions with a prediction yet'))
    expect(radio(canvasElement, 'Off').getAttribute('aria-checked')).toBe('true')
    // Keyboard path: arrows select and save the neighbour, and focus follows.
    radio(canvasElement, 'Off').focus()
    await userEvent.keyboard('{ArrowRight}')
    await waitFor(() => expect(radio(canvasElement, 'Dismiss').getAttribute('aria-checked')).toBe('true'))
    expect(document.activeElement).toBe(radio(canvasElement, 'Dismiss'))
  },
}

export const Evidence: Story = {
  parameters: {
    fixture: { routes: [readRoute(() => json(handover({ agreement: EVIDENCE })))] },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('agreed 75%'))
    expect(canvasElement.textContent).toContain('It predicted dismiss 4 times and was right 2')
    await userEvent.click(buttonNamed(canvasElement, 'Refresh'))
    await waitFor(() => expect(canvasElement.textContent).toContain('agreed 75%'))
  },
}

export const SuggestHandover: Story = {
  parameters: {
    fixture: {
      routes: [readRoute(() => json(handover({ agreement: { ...EVIDENCE, suggest_handover: true } })))],
    },
  },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="status"]')?.textContent)
      .toContain('You could let it dismiss suggestions'))
  },
}

export const CannotAct: Story = {
  parameters: {
    fixture: {
      routes: [readRoute(() => json(handover({ acting_available: false })))],
    },
  },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Handover cannot act on this install'))
    expect(radio(canvasElement, 'Off').disabled).toBe(false)
    expect(radio(canvasElement, 'Dismiss').disabled).toBe(true)
    expect(radio(canvasElement, 'Dismiss and accept').disabled).toBe(true)
  },
}

export const Stale: Story = {
  parameters: {
    fixture: {
      routes: [readRoute(firstThenFailing(handover({ agreement: EVIDENCE }), 'Synthetic refresh refused'))],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('agreed 75%'))
    await userEvent.click(buttonNamed(canvasElement, 'Refresh'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not refresh: Synthetic refresh refused. Showing the last known rate.'))
    expect(canvasElement.textContent).toContain('agreed 75%')
  },
}

export const Saving: Story = {
  parameters: {
    fixture: {
      routes: [
        readRoute(() => json(handover())),
        saveRoute(held),
      ],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(radio(canvasElement, 'Dismiss'))
    await waitFor(() => expect(radio(canvasElement, 'Dismiss').getAttribute('aria-checked')).toBe('true'))
    // While a save is in flight the options report busy without being disabled.
    for (const label of ['Off', 'Dismiss', 'Dismiss and accept']) {
      expect(radio(canvasElement, label).getAttribute('aria-disabled')).toBe('true')
      expect(radio(canvasElement, label).disabled).toBe(false)
    }
  },
}

export const SaveFailed: Story = {
  parameters: {
    fixture: {
      routes: [
        readRoute(() => json(handover())),
        saveRoute(() => json({ error: 'Synthetic save refused' }, 500)),
        registryRoute(),
      ],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(radio(canvasElement, 'Dismiss'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Synthetic save refused'))
    // The refused save hands the choice back to the saved value.
    expect(radio(canvasElement, 'Off').getAttribute('aria-checked')).toBe('true')
    expect(radio(canvasElement, 'Dismiss').getAttribute('aria-checked')).toBe('false')
  },
}
