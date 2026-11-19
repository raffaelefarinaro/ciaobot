import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import OnboardingLocationStage from '../components/onboarding/OnboardingLocationStage.vue'
import type { OnboardingStageProps } from '../lib/onboarding'
import { held, json, route, settled, stagePanel, WORKSPACE } from './onboardingStagesFixture'

/**
 * OnboardingLocationStage (the `components-onboarding-onboarding-location-stage`
 * inventory row, #3309): the folder and workspace name fields of the install's
 * first stage. The provisioned variants and the folder inspection read through
 * the #3267 boundary; the fresh stage reads nothing on mount and is only
 * driven by the change and blur events its plays perform. The `Change` links
 * are asserted, never clicked: following them would leave the workshop page
 * (provisioned) or open another input (name editing). Synthetic paths only.
 */
const meta = {
  id: 'onboarding-location-stage',
  title: 'Onboarding stable stages/OnboardingLocationStage',
  component: OnboardingLocationStage,
  // The meta args exist for the CSF type only: each story's render mounts the
  // production stage with its own synthetic context.
  args: {
    context: { target: { kind: 'installation' }, workspace: WORKSPACE, revision: 0, provisioned: false },
    busy: false,
  },
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof OnboardingLocationStage>
export default meta
type Story = StoryObj<typeof meta>

const fresh = (): OnboardingStageProps => ({
  context: { target: { kind: 'installation' }, workspace: '', revision: 0, provisioned: false },
  busy: false,
})
const provisioned = (): OnboardingStageProps => ({
  context: { target: { kind: 'installation' }, workspace: WORKSPACE, revision: 2, provisioned: true },
  busy: false,
})

const statusRoute = (respond: () => Response | Promise<never>) => route('GET', '/api/setup-status', respond)
/** The engine inspects the trimmed path on change, URL-encoded into the query. */
const inspectRoute = (path: string, respond: () => Response | Promise<never>) =>
  route('GET', `/api/setup/inspect-folder?path=${encodeURIComponent(path)}`, respond)

export const Default: Story = {
  // Fresh install: both fields empty and no transport on mount.
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: fresh() }),
  play: async ({ canvasElement }) => {
    await waitFor(() => {
      const folder = canvasElement.querySelector<HTMLInputElement>('#onboarding-folder')
      expect(folder).toBeTruthy()
      return folder!
    })
    const name = canvasElement.querySelector<HTMLInputElement>('#onboarding-name')!
    expect(name.value).toBe('')
    expect(canvasElement.querySelectorAll('.set-row')).toHaveLength(0)
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
    expect(canvasElement.textContent).toContain('Main Ciaobot folder')
    expect(canvasElement.textContent).toContain('Workspace name')
    expect(canvasElement.textContent).toContain('Holds application data, workspaces, notes, chats and settings.')
    // A fresh stage reads nothing: the boundary ledger must stay empty.
    expect(canvasElement.textContent).toContain('Fixture requests: 0.')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":false,"error":null}.')
  },
}

export const FolderInspected: Story = {
  // A changed folder is inspected before it can be carried into setup.
  parameters: { fixture: { routes: [inspectRoute('/storybook/alpha', () => json({ ok: true, mode: 'existing' }))] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: fresh() }),
  play: async ({ canvasElement, userEvent }) => {
    const folder = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLInputElement>('#onboarding-folder')
      expect(found).toBeTruthy()
      return found!
    })
    await userEvent.type(folder, '/storybook/alpha')
    // Leaving the field commits the change event that starts the inspection.
    await userEvent.click(canvasElement.querySelector<HTMLInputElement>('#onboarding-name')!)
    await waitFor(() => expect(canvasElement.textContent).toContain('Existing notes found: Ciaobot keeps files where they are and adapts around them.'))
    // The inspected folder alone does not enable Continue: the name is still empty.
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":false,"error":null}.')
    expect(canvasElement.textContent).toContain('Fixture requests: 1.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const FolderError: Story = {
  // The engine refuses the folder, or the request fails: the error blocks Continue.
  parameters: { fixture: { routes: [inspectRoute('/storybook/nope', () => json({ ok: false, error: 'That folder cannot be used.' }))] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: fresh() }),
  play: async ({ canvasElement, userEvent }) => {
    const folder = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLInputElement>('#onboarding-folder')
      expect(found).toBeTruthy()
      return found!
    })
    await userEvent.type(folder, '/storybook/nope')
    await userEvent.click(canvasElement.querySelector<HTMLInputElement>('#onboarding-name')!)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toContain('That folder cannot be used.'))
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":false,"error":"That folder cannot be used."}.')
    expect(canvasElement.textContent).toContain('Fixture requests: 1.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const NameInvalid: Story = {
  // The engine's own name contract, enforced on blur: the owner cannot strand
  // a workspace on a name the engine would refuse.
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: fresh() }),
  play: async ({ canvasElement, userEvent }) => {
    const name = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLInputElement>('#onboarding-name')
      expect(found).toBeTruthy()
      return found!
    })
    await userEvent.type(name, 'bad name')
    await userEvent.click(canvasElement.querySelector<HTMLInputElement>('#onboarding-folder')!)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toContain('Use letters, numbers, dashes or underscores; start with a letter or number.'))
    expect(name.getAttribute('aria-invalid')).toBe('true')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":true,"pending":false,"canContinue":false,"error":"Use letters, numbers, dashes or underscores; start with a letter or number."}.')
    expect(canvasElement.textContent).toContain('Fixture requests: 0.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}

export const Provisioned: Story = {
  // The installer already registered the workspace: set rows instead of fields.
  parameters: { fixture: { routes: [statusRoute(() => json({ workspace_root: '/storybook/alpha' }))] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: provisioned() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('/storybook/alpha'))
    expect(canvasElement.textContent).toContain('Ciaobot folder')
    expect(canvasElement.textContent).toContain(`Workspace`)
    expect(canvasElement.querySelector<HTMLInputElement>('.set-value')!.textContent).toBe('/storybook/alpha')
    // The Change link is asserted, never clicked: it would leave the workshop page.
    const link = canvasElement.querySelector<HTMLAnchorElement>('a.change')!
    expect(link.getAttribute('href')).toBe('/settings/advanced#workspace-move')
    expect(canvasElement.querySelector<HTMLButtonElement>('button.change-name')?.textContent?.trim()).toBe('Change')
    expect(canvasElement.querySelectorAll('input')).toHaveLength(0)
    expect(canvasElement.textContent).not.toContain('Loading…')
  },
}

export const ProvisionedLoading: Story = {
  // The held status read: the set row keeps its loaded geometry and the value
  // reads Loading until the engine answers.
  parameters: { fixture: { routes: [statusRoute(held)] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: provisioned() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.set-value')?.textContent).toBe('Loading…'))
    expect(canvasElement.textContent).toContain('Ciaobot folder')
    expect(canvasElement.textContent).not.toContain('/storybook')
    expect(canvasElement.querySelectorAll('[role="alert"]')).toHaveLength(0)
  },
}

export const ProvisionedLoadError: Story = {
  // A failed status read: the alert renders beside the still-Loading value.
  parameters: { fixture: { routes: [statusRoute(() => json({ error: 'Synthetic refusal' }, 503))] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: provisioned() }),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent).toBe('Could not load the current Ciaobot folder.'))
    expect(canvasElement.querySelector('.set-value')!.textContent).toBe('Loading…')
    expect(canvasElement.textContent).toContain('StageState: {"dirty":false,"pending":false,"canContinue":false,"error":"Could not load the current Ciaobot folder."}.')
  },
}

export const ProvisionedEditName: Story = {
  // Change (the link button) asks the name again, pre-filled with the stored one.
  parameters: { fixture: { routes: [statusRoute(() => json({ workspace_root: '/storybook/alpha' }))] } },
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: provisioned() }),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const change = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLButtonElement>('button.change-name')
      expect(found).toBeTruthy()
      return found!
    })
    await userEvent.click(change)
    await waitFor(() => {
      const found = canvasElement.querySelector<HTMLInputElement>('#onboarding-name')
      expect(found).toBeTruthy()
      return found!
    })
    const name = canvasElement.querySelector<HTMLInputElement>('#onboarding-name')!
    expect(name.value).toBe(WORKSPACE)
    expect(canvasElement.textContent).toContain('Workspace name')
    // The folder set row stays while the name is edited.
    expect(canvasElement.textContent).toContain('Ciaobot folder')
    expect(canvasElement.querySelector('.set-value')!.textContent).toBe('/storybook/alpha')
  },
}

export const Narrow: Story = {
  // The fresh stage in the 390px container: one column of fields, no set rows.
  render: () => stagePanel({ label: 'location', component: OnboardingLocationStage, props: fresh(), narrow: true }),
  play: async ({ canvasElement }) => {
    const stage = await waitFor(() => {
      const found = canvasElement.querySelector<HTMLElement>('.location-stage')
      expect(found).toBeTruthy()
      return found!
    })
    expect(stage.getBoundingClientRect().width).toBeLessThanOrEqual(391)
    expect(canvasElement.querySelector<HTMLInputElement>('#onboarding-folder')).toBeTruthy()
    expect(canvasElement.querySelector<HTMLInputElement>('#onboarding-name')).toBeTruthy()
    expect(canvasElement.textContent).toContain('Fixture requests: 0.')
    expect(canvasElement.textContent).toMatch(/Violations: 0\./)
  },
}
