import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, waitFor } from 'storybook/test'
import SettingsMemoryBackup from '../components/settings/SettingsMemoryBackup.vue'
import { firstThenFailing, held, json, ownerPanel, route, settled } from './settingsAccessFixture'

/**
 * SettingsMemoryBackup (the `components-settings-settings-memory-backup`
 * inventory row, #3307): the memory backup card. It reads `/api/local/backup`
 * on mount and writes through the four backup routes below, all through the
 * #3267 boundary. Every story declares exactly the routes its play performs.
 * The remote is a reserved `.invalid` host; no real repository or token.
 */
const meta = {
  id: 'settings-memory-backup',
  title: 'Settings access and memory/SettingsMemoryBackup',
  component: SettingsMemoryBackup,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
  },
} satisfies Meta<typeof SettingsMemoryBackup>
export default meta
type Story = StoryObj<typeof meta>

const READY = {
  state: 'ready',
  scope: 'memory',
  branch: 'main',
  remote: 'https://example.invalid/storybook-alpha/memory.git',
  last_remote: '',
  enabled: true,
  interval_s: 300,
  last_attempt_at: '2026-03-01T09:00:00+00:00',
  last_success_at: '2026-03-01T09:00:00+00:00',
  last_success_commit: 'storybook0001',
  pending_changes: 0,
  pending_commits: 0,
  coverage_gap: 0,
  reason: '',
}

/** One `GET /api/local/backup` answer: the ready status with the given fields changed. */
const backup = (overrides: Record<string, unknown> = {}) => ({ ...READY, ...overrides })

const UNCONFIGURED = backup({
  state: 'not_configured',
  enabled: false,
  remote: '',
  last_attempt_at: '',
  last_success_at: '',
  last_success_commit: '',
})

const statusRoute = (respond: () => Response | Promise<never>) =>
  route('GET', '/api/local/backup', respond)
const panel = (clipboard = false) =>
  ownerPanel({ label: 'SettingsMemoryBackup', component: SettingsMemoryBackup, clipboard })

function buttonNamed(canvasElement: HTMLElement, name: string): HTMLButtonElement {
  const found = [...canvasElement.querySelectorAll('button')].find((button) => button.textContent?.trim() === name)
  if (!found) throw new Error(`No button named ${name}`)
  return found
}

export const Loading: Story = {
  parameters: { fixture: { routes: [statusRoute(held)] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Checking your backup'))
    expect(canvasElement.querySelector('.backup-status')).toBeNull()
  },
}

export const Failed: Story = {
  parameters: {
    fixture: {
      routes: [statusRoute(() => json({ error: 'Synthetic status refused' }, 500))],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the backup status: Synthetic status refused'))
    await userEvent.click(buttonNamed(canvasElement, 'Check again'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not read the backup status'))
    expect(canvasElement.querySelector('.backup-status')).toBeNull()
  },
}

export const NotConfigured: Story = {
  parameters: { fixture: { routes: [statusRoute(() => json(UNCONFIGURED))] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.textContent).toContain('Your memory is saved on this device'))
    expect(buttonNamed(canvasElement, 'Set up in Ciaobot').disabled).toBe(false)
    expect(buttonNamed(canvasElement, 'Copy setup prompt').disabled).toBe(false)
  },
}

export const SetupStarted: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(UNCONFIGURED)),
        route('POST', '/api/local/backup/setup-chat', () => json({ ok: true, chat_id: 'storybook-setup-chat', reused: false })),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Set up in Ciaobot'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Started a setup chat.'))
    await userEvent.click(buttonNamed(canvasElement, 'Open the setup chat'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Open chat emit: storybook-setup-chat'))
  },
}

export const SetupFailed: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(UNCONFIGURED)),
        route('POST', '/api/local/backup/setup-chat', () => json({ error: 'Synthetic setup refused' }, 500)),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Set up in Ciaobot'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Synthetic setup refused'))
    expect(canvasElement.textContent).not.toContain('Started a setup chat.')
  },
}

export const SetupPromptCopied: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(UNCONFIGURED)),
        route('GET', '/api/local/backup/setup-prompt', () => json({ prompt: 'Synthetic setup prompt' })),
      ],
    },
  },
  render: () => panel(true),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Copy setup prompt'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Setup prompt copied.'))
  },
}

export const Ready: Story = {
  parameters: { fixture: { routes: [statusRoute(() => json(READY))] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Backed up'))
    expect(buttonNamed(canvasElement, 'Pause')).toBeTruthy()
    expect(buttonNamed(canvasElement, 'Back up now')).toBeTruthy()
  },
}

export const Paused: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(backup({ state: 'paused' }))),
        route('PATCH', '/api/local/backup', () => json(READY)),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Resume'))
    await waitFor(() => expect(canvasElement.textContent).toContain('Automatic backups are running again.'))
    expect(buttonNamed(canvasElement, 'Pause')).toBeTruthy()
  },
}

export const Running: Story = {
  parameters: { fixture: { routes: [statusRoute(() => json(backup({ state: 'running' })))] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Backing up now'))
  },
}

export const Offline: Story = {
  parameters: {
    fixture: {
      routes: [statusRoute(() => json(backup({ state: 'offline', reason: 'Synthetic remote unreachable' })))],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Offline'))
    expect(canvasElement.querySelector('.backup-reason')?.textContent).toContain('Synthetic remote unreachable')
  },
}

export const NeedsAttention: Story = {
  parameters: {
    fixture: {
      routes: [statusRoute(() => json(backup({ state: 'needs_attention', reason: 'Synthetic push rejected' })))],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    const details = canvasElement.querySelector('details') as HTMLDetailsElement
    expect(details.open).toBe(false)
    await userEvent.click(canvasElement.querySelector('summary') as HTMLElement)
    expect(details.open).toBe(true)
    expect(canvasElement.querySelector('.backup-reason')?.textContent).toContain('Synthetic push rejected')
  },
}

export const Deferred: Story = {
  parameters: { fixture: { routes: [statusRoute(() => json(backup({ state: 'deferred' })))] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Backup deferred'))
    expect(canvasElement.textContent).toContain('A Ciaobot update is in progress')
  },
}

export const Pending: Story = {
  parameters: {
    fixture: { routes: [statusRoute(() => json(backup({ state: 'pending', pending_commits: 2 })))] },
  },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Waiting to upload'))
  },
}

export const ScopeGap: Story = {
  parameters: { fixture: { routes: [statusRoute(() => json(backup({ coverage_gap: 2 })))] } },
  render: () => panel(),
  play: async ({ canvasElement }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-warning')?.textContent)
      .toContain('2 tracked files are outside the backup scope'))
    expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Backed up')
  },
}

export const Stale: Story = {
  parameters: {
    fixture: {
      routes: [statusRoute(firstThenFailing(backup({ state: 'paused' }), 'Synthetic refresh refused'))],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await waitFor(() => expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Paused'))
    await userEvent.click(canvasElement.querySelector('.backup-recheck') as HTMLElement)
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('Could not refresh: Synthetic refresh refused. Showing the last known status.'))
    expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Paused')
  },
}

export const RunPending: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(READY)),
        route('POST', '/api/local/backup/run', held),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Back up now'))
    await waitFor(() => expect(buttonNamed(canvasElement, 'Backing up...').disabled).toBe(true))
  },
}

export const RunFailed: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(READY)),
        route('POST', '/api/local/backup/run', () => json(backup({ state: 'offline', reason: 'Synthetic remote unreachable' }), 400)),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Back up now'))
    await waitFor(() => expect(canvasElement.querySelector('[role="alert"]')?.textContent)
      .toContain('The backup could not run. Your memory is still safe on this computer. Synthetic remote unreachable'))
    expect(canvasElement.querySelector('.backup-state-line')?.textContent).toContain('Offline')
  },
}

export const FlagPending: Story = {
  parameters: {
    fixture: {
      routes: [
        statusRoute(() => json(READY)),
        route('PATCH', '/api/local/backup', held),
      ],
    },
  },
  render: () => panel(),
  play: async ({ canvasElement, userEvent }) => {
    await settled(canvasElement)
    await userEvent.click(buttonNamed(canvasElement, 'Pause'))
    await waitFor(() => expect(buttonNamed(canvasElement, 'Working...').disabled).toBe(true))
  },
}
