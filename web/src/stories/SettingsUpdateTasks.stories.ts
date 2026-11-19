import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsUpdateTasks from '../components/settings/SettingsUpdateTasks.vue'
import type { UpdateTaskRow } from '../lib/types'
import { firstThenFailing, held, json, ownerPanel, route, seedWorkspace, settled, WORKSPACE } from './settingsAccessFixture'

/**
 * SettingsUpdateTasks (the `components-settings-settings-update-tasks`
 * inventory row, #3307): the update task history in Workspace overview. It
 * reads `/api/update-tasks` for the active workspace on mount and POSTs a
 * reopen, both through the #3267 boundary. Every row is synthetic.
 */
const meta = {
  id: 'settings-update-tasks',
  title: 'Settings access and memory/SettingsUpdateTasks',
  component: SettingsUpdateTasks,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsUpdateTasks>
export default meta
type Story = StoryObj<typeof meta>

const row = (overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow => ({
  id: 'sb-update-workspace',
  revision: 1,
  scope: 'workspace',
  title: 'Synthetic workspace update',
  why: 'Synthetic reason for this update task.',
  since_version: '2.0.0',
  status: 'failed',
  applicability: 'applicable',
  applicability_checked_at: '2026-03-01T09:00:00+00:00',
  offered: false,
  suppressed: false,
  chat_id: '',
  chat_live: false,
  prompt_digest: '',
  attempted_fingerprint: '',
  updated_at: '2026-03-01T09:00:00+00:00',
  ...overrides,
})

const installHidden = row({ id: 'sb-update-install', scope: 'install', title: 'Synthetic install update', status: 'dismissed' })
const workspaceFailed = row({ id: 'sb-update-failed', title: 'Synthetic failed update', status: 'failed' })
const workspaceDone = row({ id: 'sb-update-done', title: 'Synthetic finished update', status: 'completed' })
const reopenable = row({ id: 'sb-update-reopen', title: 'Synthetic reopenable update', status: 'dismissed' })
const liveChat = row({
  id: 'sb-update-chat',
  title: 'Synthetic live chat update',
  status: 'failed',
  chat_id: 'sb-update-chat-live',
  chat_live: true,
})

const listRoute = (respond: () => Response | Promise<never>) =>
  route('GET', `/api/update-tasks?workspace=${WORKSPACE}`, respond)
const tasks = (rows: UpdateTaskRow[]) => () => json({ tasks: rows })

const panel = () => ownerPanel({ label: 'SettingsUpdateTasks', component: SettingsUpdateTasks, seed: seedWorkspace })

function buttonNamed(canvasElement: HTMLElement, name: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === name)
  if (!found) throw new Error(`No button named ${name}`)
  return found
}

export const Loading: Story = {
  parameters: { fixture: { routes: [listRoute(held)] } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Checking the update task history'))
    expect(canvasElement.querySelector('.set-list')).toBeNull()
  },
}

export const Empty: Story = {
  parameters: { fixture: { routes: [listRoute(tasks([]))] } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Nothing to show here yet.'))
    expect(canvasElement.querySelector('.set-list')).toBeNull()
  },
}

export const Default: Story = {
  parameters: { fixture: { routes: [listRoute(tasks([installHidden, workspaceFailed, workspaceDone]))] } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic failed update'))
    const labels = [...canvasElement.querySelectorAll('.set-group-label')].map((label) => label.textContent?.trim())
    expect(labels).toEqual(['This install', `This workspace (${WORKSPACE})`])
    expect(buttonNamed(canvasElement, 'Reopen')).toBeTruthy()
    expect(buttonNamed(canvasElement, 'Recheck')).toBeTruthy()
    // Finished work is collapsed until asked for.
    expect(canvasElement.textContent).not.toContain('Synthetic finished update')
    expect(buttonNamed(canvasElement, 'Show 1 done').getAttribute('aria-expanded')).toBe('false')
  },
}

export const DoneShown: Story = {
  parameters: { fixture: { routes: [listRoute(tasks([installHidden, workspaceFailed, workspaceDone]))] } },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Show 1 done'))
    const toggle = buttonNamed(canvasElement, 'Show 1 done')
    await userEvent.click(toggle)
    await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic finished update'))
    expect(buttonNamed(canvasElement, 'Hide done').getAttribute('aria-expanded')).toBe('true')
  },
}

export const LiveChat: Story = {
  parameters: { fixture: { routes: [listRoute(tasks([liveChat]))] } },
  render: panel,
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic live chat update'))
    // The chat link is asserted, not clicked: following it would push a route into the Storybook page.
    expect(buttonNamed(canvasElement, 'Open its chat').disabled).toBe(false)
  },
}

export const ReopenPending: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute(tasks([reopenable])),
        route('POST', `/api/update-tasks/${reopenable.id}/reopen?workspace=${WORKSPACE}`, held),
      ],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Reopen'))
    await waitFor(() => expect(buttonNamed(canvasElement, 'Reopening…').disabled).toBe(true))
  },
}

export const ReopenRefused: Story = {
  parameters: {
    fixture: {
      routes: [
        listRoute(tasks([reopenable])),
        route('POST', `/api/update-tasks/${reopenable.id}/reopen?workspace=${WORKSPACE}`, () => json({ error: 'Synthetic refusal' }, 409)),
      ],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Reopen'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not reopen it: the engine declined, so nothing was changed.'))
    expect(canvasElement.textContent).toContain('Synthetic reopenable update')
  },
}

export const RefreshFailed: Story = {
  parameters: {
    fixture: {
      routes: [listRoute(firstThenFailing({ tasks: [workspaceFailed] }, 'Synthetic list refused'))],
    },
  },
  render: panel,
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Synthetic failed update'))
    await userEvent.click(buttonNamed(canvasElement, 'Check again'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the update task history: Synthetic list refused'))
    // The last known rows stay on screen under the failure.
    expect(canvasElement.textContent).toContain('Synthetic failed update')
  },
}
