import { h } from 'vue'
import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsCareCycle from '../components/settings/SettingsCareCycle.vue'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import type { CareCycleReport } from '../lib/types'
import { held, json, ownerPanel, route, seedWorkspace, settled, WORKSPACE } from './settingsAccessFixture'

/**
 * SettingsCareCycle (the `components-settings-settings-care-cycle` inventory
 * row, #3061): the one-way switch of a workspace's care state to the weekly
 * cycle, on Workspace > Overview. It reads `/api/care/cycle` for the active
 * workspace and posts the switch only after the checkbox and the confirm. The
 * confirm dialog is mounted beside the card, because the workshop has no
 * app-level confirm host. Synthetic workspace, paths and counts only.
 */
const meta = {
  id: 'settings-care-cycle',
  title: 'Settings access and memory/SettingsCareCycle',
  component: SettingsCareCycle,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsCareCycle>
export default meta
type Story = StoryObj<typeof meta>

const BACKUP = `/storybook/alpha/Workspace/Curation-State.v1.bak`
const DIGEST = 'storybook-digest-1'

const report = (overrides: Partial<CareCycleReport> = {}): CareCycleReport => ({
  status: 'v1',
  live_lease: null,
  marker: 'present',
  last_full_pass: '2026-11-09',
  daily: 2,
  weekly_historical: 3,
  ambiguous: [],
  dropped: [],
  backup_path: BACKUP,
  digest: DIGEST,
  blockers: [],
  ...overrides,
})

const hostRead = (overrides: Partial<CareCycleReport> = {}) => ({
  workspace: WORKSPACE,
  on_host: true,
  lease_live: false,
  ...report(overrides),
})

const readRoute = (respond: () => Response | Promise<never>) =>
  route('GET', `/api/care/cycle?workspace=${WORKSPACE}`, respond)
const applyRoute = (respond: () => Response | Promise<never>) =>
  route('POST', '/api/care/cycle', respond)

// After a switch the card re-reads the preview: the first read is the ready
// preview, every later read is the switched vault.
function readThenSwitched(first: unknown, after: unknown): () => Response {
  let calls = 0
  return () => {
    calls += 1
    return json(calls === 1 ? first : after)
  }
}

const panel = (clipboard = false) => () => h('div', [
  ownerPanel({ label: 'SettingsCareCycle', component: SettingsCareCycle, seed: seedWorkspace, clipboard }),
  h(ConfirmDialog),
])

function buttonNamed(canvasElement: HTMLElement, name: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === name)
  if (!found) throw new Error(`No button named ${name}`)
  return found
}

function checkbox(canvasElement: HTMLElement): HTMLInputElement {
  const found = canvasElement.querySelector('input[type="checkbox"]')
  if (!found) throw new Error('No confirmation checkbox')
  return found as HTMLInputElement
}

export const Loading: Story = {
  parameters: { fixture: { routes: [readRoute(held)] } },
  render: panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Checking the care state'))
    expect([...canvasElement.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Switch this workspace')).toBe(false)
  },
}

export const Failed: Story = {
  parameters: {
    fixture: { routes: [readRoute(() => json({ error: 'Synthetic care state refused' }, 500))] },
  },
  render: panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the care state: Synthetic care state refused'))
    await userEvent.click(buttonNamed(canvasElement, 'Try again'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the care state'))
  },
}

export const Ready: Story = {
  parameters: { fixture: { routes: [readRoute(() => json(hostRead()))] } },
  render: panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('2 daily progress records kept.'))
    expect(canvasElement.textContent).toContain('Stop other copies first.')
    expect(canvasElement.textContent).toContain(BACKUP)
    // The switch stays aria-disabled until the checkbox is ticked.
    expect(buttonNamed(canvasElement, 'Switch this workspace').getAttribute('aria-disabled')).toBe('true')
    await userEvent.click(checkbox(canvasElement))
    await waitFor(() => expect(buttonNamed(canvasElement, 'Switch this workspace').getAttribute('aria-disabled')).toBeNull())
  },
}

export const BlockedByLease: Story = {
  parameters: {
    fixture: {
      routes: [readRoute(() => json({
        ...hostRead(),
        lease_live: true,
        live_lease: { holder: 'storybook run', started_at: '2026-11-21T01:00:00Z', expires_at: '2026-11-21T02:00:00Z' },
        blockers: ['a live curation lease holds this vault; wait for the run to finish or expire'],
      }))],
    },
  },
  render: panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('A care run is in progress. Wait for it to finish.'))
    const button = buttonNamed(canvasElement, 'Switch this workspace')
    expect(button.getAttribute('aria-disabled')).toBe('true')
    expect(button.getAttribute('aria-describedby')).toBe('care-cycle-reason')
  },
}

export const NotOnHost: Story = {
  parameters: {
    fixture: {
      routes: [readRoute(() => json({ workspace: WORKSPACE, on_host: false, reason: 'not_on_host', lease_live: null }))],
    },
  },
  render: panel(true),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('This switch runs on the computer running Ciaobot'))
    expect([...canvasElement.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Switch this workspace')).toBe(false)
    await userEvent.click(buttonNamed(canvasElement, 'Copy'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Copied.'))
  },
}

export const AlreadySwitched: Story = {
  parameters: {
    fixture: { routes: [readRoute(() => json(hostRead({ status: 'v2' })))] },
  },
  render: panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('This workspace already uses the weekly cycle.'))
    expect([...canvasElement.querySelectorAll('button')].some((b) => b.textContent?.trim() === 'Switch this workspace')).toBe(false)
  },
}

export const Applied: Story = {
  parameters: {
    fixture: {
      routes: [
        readRoute(readThenSwitched(hostRead(), hostRead({ status: 'v2' }))),
        applyRoute(() => json({ workspace: WORKSPACE, status: 'migrated', backup_path: BACKUP, digest: DIGEST })),
      ],
    },
  },
  render: panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(checkbox(canvasElement))
    await userEvent.click(buttonNamed(canvasElement, 'Switch this workspace'))
    // The confirm dialog names the warning before anything is posted.
    await waitFor(() => expect(document.body.textContent).toContain('Switch Workspace care to the new weekly cycle?'))
    await userEvent.click(buttonNamed(document.body as HTMLElement, 'Switch'))
    await waitFor(() => expect(canvasElement.querySelector('[role="status"]')?.textContent)
      .toContain(`Switched. A copy of the old state is saved at ${BACKUP}.`))
    expect(canvasElement.textContent).toContain('This workspace already uses the weekly cycle.')
  },
}

export const ApplyRefused: Story = {
  parameters: {
    fixture: {
      routes: [
        readRoute(() => json(hostRead())),
        applyRoute(() => json({
          error: 'The switch was refused and nothing was written.',
          blockers: ['the vault changed since the dry run (digest mismatch); run the dry run again'],
        }, 409)),
      ],
    },
  },
  render: panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(checkbox(canvasElement))
    await userEvent.click(buttonNamed(canvasElement, 'Switch this workspace'))
    await waitFor(() => expect(document.body.textContent).toContain('Switch Workspace care to the new weekly cycle?'))
    await userEvent.click(buttonNamed(document.body as HTMLElement, 'Switch'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('The switch was refused and nothing was written.'))
    expect(canvasElement.querySelector('[role="alert"]')?.textContent).toContain('digest mismatch')
  },
}
