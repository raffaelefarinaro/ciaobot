// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ProviderInstallBlock from '../settings/ProviderInstallBlock.vue'
import ProviderStateBadge from '../settings/ProviderStateBadge.vue'
import type { ProviderConnection, ProviderState } from '../../lib/types'

const CLAUDE_GUIDE = 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code'
const CODEX_GUIDE = 'https://developers.openai.com/codex/cli/'

function row(state: ProviderState, overrides: Partial<ProviderConnection> = {}): ProviderConnection {
  return {
    name: 'claude',
    label: 'Claude Code',
    ok: state === 'ready',
    auth: state === 'ready' ? 'oauth' : state === 'failed' ? 'missing' : 'not_installed',
    state,
    command: 'curl -fsSL https://claude.ai/install.sh | bash',
    detail: 'Claude Code is not installed on this machine.',
    version: state === 'not_installed' ? 'not installed' : '2.1.326 (Claude Code)',
    install_url: 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code',
    terminal_action: 'claude-install',
    ...overrides,
  } as ProviderConnection
}

/** One Settings row as SettingsView renders it: the install block and the state badge. */
function mountRow(connKey: string, conn: ProviderConnection) {
  return mount({
    components: { ProviderInstallBlock, ProviderStateBadge },
    template: `<div>
      <ProviderInstallBlock :conn-key="connKey" :conn="conn" />
      <ProviderStateBadge :conn-key="connKey" :state="conn.state" />
    </div>`,
    data: () => ({ connKey, conn }),
  }, { global: { stubs: { HostTerminalButton: { props: ['action'], template: '<button class="host-terminal-stub">Install in Terminal</button>' } } } })
}

describe('Settings provider states', () => {
  it('a CLI that is not installed reads as a neutral Not installed with a guide and a Terminal install', () => {
    const wrapper = mountRow('claude', row('not_installed'))

    expect(wrapper.get('.provider-state').text()).toBe('Not installed')
    expect(wrapper.get('.provider-state').classes()).toContain('provider-state--neutral')
    expect(wrapper.text()).not.toContain('Not connected')
    expect(wrapper.get('a').attributes('href')).toBe(CLAUDE_GUIDE)
    expect(wrapper.get('a').attributes('target')).toBe('_blank')
    expect(wrapper.get('.host-terminal-stub').text()).toBe('Install in Terminal')
  })

  it('a CLI with its adapter missing keeps the red Not connected state and the npm line', () => {
    const wrapper = mountRow('claude', row('adapter_missing', {
      command: 'npm install -g @agentclientprotocol/claude-agent-acp@0.108.1',
      detail: 'Claude Code is installed, but the ACP adapter (claude-agent-acp) is not. Install the ACP adapter with the button below, or run: npm install -g @agentclientprotocol/claude-agent-acp@0.108.1',
      version: '2.1.326 (Claude Code)',
      install_url: undefined,
      terminal_action: 'claude-install',
      auth: 'not_installed',
    }))

    expect(wrapper.get('.provider-state').text()).toBe('Not connected')
    expect(wrapper.get('.provider-state').classes()).toContain('provider-state--error')
    expect(wrapper.text()).toContain('npm install -g @agentclientprotocol/claude-agent-acp@0.108.1')
    expect(wrapper.text()).toContain('button below')
    expect(wrapper.find('a').exists()).toBe(false)
    expect(wrapper.find('.host-terminal-stub').exists()).toBe(true)
  })

  it('a ready CLI shows Connected and no install step', () => {
    const wrapper = mountRow('claude', row('ready'))

    expect(wrapper.get('.provider-state').text()).toBe('Connected')
    expect(wrapper.get('.provider-state').classes()).toContain('provider-state--ok')
    expect(wrapper.text()).not.toContain('Install')
    expect(wrapper.find('a').exists()).toBe(false)
  })

  it('a failed CLI shows Not connected with no install step, because it is installed', () => {
    const wrapper = mountRow('claude', row('failed'))

    expect(wrapper.get('.provider-state').text()).toBe('Not connected')
    expect(wrapper.get('.provider-state').classes()).toContain('provider-state--error')
    expect(wrapper.find('.host-terminal-stub').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Install it with')
  })

  it('Codex not installed links its own install guide, and opencode says CLI not ready only when failed', () => {
    const codex = mountRow('codex', row('not_installed', {
      name: 'codex',
      label: 'Codex',
      install_url: CODEX_GUIDE,
      terminal_action: 'codex-install',
      command: 'npm install -g @openai/codex@0.166.0 @agentclientprotocol/codex-acp@2.7.0',
    }))
    expect(codex.get('a').attributes('href')).toBe(CODEX_GUIDE)
    expect(codex.get('.provider-state').text()).toBe('Not installed')

    const opencode = mountRow('opencode', row('failed', { name: 'opencode', label: 'OpenCode', auth: 'unknown', terminal_action: undefined }))
    expect(opencode.get('.provider-state').text()).toBe('CLI not ready')
  })
})
