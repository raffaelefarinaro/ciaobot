import type { Meta, StoryObj } from '@storybook/vue3-vite'
import { expect, screen } from 'storybook/test'
import { h } from 'vue'
import ProviderInstallBlock from '../components/settings/ProviderInstallBlock.vue'
import ProviderStateBadge from '../components/settings/ProviderStateBadge.vue'
import type { FixtureRoute } from './fixtureBoundary'
import { resetHostTerminalForTests } from '../composables/useHostTerminal'
import type { ProviderConnection, ProviderState } from '../lib/types'

/**
 * The Settings providers row's status pieces (the `settings-provider-state-badge`
 * and `settings-provider-install-block` inventory rows): the state mark and
 * the install step a missing CLI gets. Every row is synthetic: no engine, the
 * host-terminal capability is answered by the fixture so the Install in
 * Terminal button renders as it does on the computer running Ciaobot.
 */

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

const HOST_TERMINAL: FixtureRoute = {
  method: 'GET',
  path: '/api/host/terminal',
  respond: () => json({
    available: true,
    platform: 'mac',
    reason: '',
    actions: ['claude-install', 'codex-install', 'opencode-install'],
  }),
}

const CLAUDE_GUIDE = 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code'

function connection(state: ProviderState, overrides: Partial<ProviderConnection> = {}): ProviderConnection {
  const base: Record<ProviderState, Partial<ProviderConnection>> = {
    not_installed: {
      ok: false, auth: 'not_installed', version: 'not installed',
      detail: 'Claude Code is not installed on this machine.',
      command: 'curl -fsSL https://claude.ai/install.sh | bash',
      install_url: CLAUDE_GUIDE, terminal_action: 'claude-install',
    },
    adapter_missing: {
      ok: false, auth: 'not_installed', version: '2.1.326 (Claude Code)',
      detail: 'Claude Code is installed, but the ACP adapter (claude-agent-acp) is not. Install the ACP adapter with the button below, or run: npm install -g @agentclientprotocol/claude-agent-acp@0.108.1',
      command: 'npm install -g @agentclientprotocol/claude-agent-acp@0.108.1',
      terminal_action: 'claude-install',
    },
    ready: { ok: true, auth: 'oauth', version: '2.1.326 (Claude Code)', detail: 'Signed in as user@example.test', command: 'claude auth login' },
    failed: { ok: false, auth: 'missing', version: '2.1.326 (Claude Code)', detail: 'Run Claude OAuth.', command: 'claude auth login' },
  }
  return { name: 'claude', label: 'Claude Code', state, ...base[state], ...overrides } as ProviderConnection
}

const meta = {
  id: 'provider-status-row',
  title: 'Settings providers/ProviderStatusRow',
  component: ProviderInstallBlock,
  tags: ['!autodocs'],
  parameters: {
    docs: { disable: true, story: { inline: false } },
    controls: { disable: true },
    fixture: { routes: [HOST_TERMINAL] },
  },
  args: { connKey: 'claude', conn: connection('ready') },
  render: (args) => installRender(args.connKey, args.conn),
} satisfies Meta<typeof ProviderInstallBlock>
export default meta
type Story = StoryObj<typeof meta>

const badge = (canvasElement: HTMLElement) => canvasElement.querySelector('.provider-state')

// The host-terminal capability is cached per page; reset it so each story's
// Install in Terminal button makes its own declared read.
function installRender(connKey: string, conn: ProviderConnection) {
  resetHostTerminalForTests()
  return h(ProviderInstallBlock, { connKey, conn })
}

// The state mark on its own: the words and tone the card's row shows.
const badgeOf = (state: ProviderState) => ({
  args: { connKey: 'claude', conn: connection(state) },
  render: (args: { connKey: string, conn: ProviderConnection }) => h(ProviderStateBadge, { connKey: args.connKey, state: args.conn.state }),
})

export const BadgeNotInstalled: Story = {
  ...badgeOf('not_installed'),
  play: async ({ canvasElement }) => {
    expect(badge(canvasElement)?.textContent).toBe('Not installed')
    expect(badge(canvasElement)?.classList.contains('provider-state--neutral')).toBe(true)
    expect(canvasElement.textContent).not.toContain('Not connected')
  },
}

export const BadgeAdapterMissing: Story = {
  ...badgeOf('adapter_missing'),
  play: async ({ canvasElement }) => {
    expect(badge(canvasElement)?.textContent).toBe('Not connected')
    expect(badge(canvasElement)?.classList.contains('provider-state--error')).toBe(true)
  },
}

export const BadgeReady: Story = {
  ...badgeOf('ready'),
  play: async ({ canvasElement }) => {
    expect(badge(canvasElement)?.textContent).toBe('Connected')
    expect(badge(canvasElement)?.classList.contains('provider-state--ok')).toBe(true)
  },
}

export const BadgeFailed: Story = {
  ...badgeOf('failed'),
  play: async ({ canvasElement }) => {
    expect(badge(canvasElement)?.textContent).toBe('Not connected')
    expect(badge(canvasElement)?.classList.contains('provider-state--error')).toBe(true)
  },
}

// The install step: rendered only for a missing CLI or adapter.
export const InstallNotInstalled: Story = {
  args: { connKey: 'claude', conn: connection('not_installed') },
  render: (args) => installRender(args.connKey, args.conn),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('Claude Code is not installed on this machine.')
    expect(screen.getByRole('link', { name: 'installation guide ↗' }).getAttribute('href')).toBe(CLAUDE_GUIDE)
    expect(await screen.findByRole('button', { name: 'Install Claude Code in Terminal' })).toBeTruthy()
  },
}

export const InstallAdapterMissing: Story = {
  args: { connKey: 'claude', conn: connection('adapter_missing') },
  render: (args) => installRender(args.connKey, args.conn),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).toContain('npm install -g @agentclientprotocol/claude-agent-acp@0.108.1')
    expect(canvasElement.querySelector('a')).toBeNull()
    expect(await screen.findByRole('button', { name: 'Install Claude Code in Terminal' })).toBeTruthy()
  },
}

export const InstallReady: Story = {
  args: { connKey: 'claude', conn: connection('ready') },
  render: (args) => installRender(args.connKey, args.conn),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).not.toContain('Install')
    expect(canvasElement.querySelector('a')).toBeNull()
  },
}

export const InstallFailed: Story = {
  args: { connKey: 'claude', conn: connection('failed') },
  render: (args) => installRender(args.connKey, args.conn),
  play: async ({ canvasElement }) => {
    expect(canvasElement.textContent).not.toContain('Install')
    expect(canvasElement.querySelector('button')).toBeNull()
  },
}
