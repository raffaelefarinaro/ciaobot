// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import ProviderMcpButton from '../ProviderMcpButton.vue'
import type { ProviderConnection } from '../../lib/types'

const runMock = vi.hoisted(() => vi.fn())
vi.mock('../../composables/useRunInTerminal', () => ({
  useRunInTerminal: () => ({ run: runMock, busy: ref(false) }),
}))

function connection(over: Partial<ProviderConnection> = {}): ProviderConnection {
  return {
    name: 'claude',
    label: 'Claude Code',
    ok: true,
    auth: 'ok',
    state: 'ready',
    command: 'claude auth login',
    mcp_command: 'claude',
    mcp_slash: '/mcp',
    ...over,
  }
}

function mountButton(props: { connection: ProviderConnection }) {
  return mount(ProviderMcpButton, { props })
}

describe('ProviderMcpButton', () => {
  afterEach(() => { runMock.mockReset() })

  it('renders only the button, labelled with the agent', () => {
    const wrapper = mountButton({ connection: connection() })
    expect(wrapper.findAll('button')).toHaveLength(1)
    expect(wrapper.get('button').text()).toBe('Open MCP settings in Claude Code')
    expect(wrapper.find('code').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('claude')
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('launches mcp_command and then types mcp_slash in the project terminal', async () => {
    runMock.mockResolvedValue('typed')
    const wrapper = mountButton({ connection: connection() })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith('claude', { followUp: '/mcp' })
  })

  it('shows Codex the same way, with its own launch command', async () => {
    runMock.mockResolvedValue('typed')
    const wrapper = mountButton({
      connection: connection({ name: 'codex', label: 'Codex', mcp_command: "'/x y/codex'" }),
    })
    expect(wrapper.get('button').text()).toBe('Open MCP settings in Codex')
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith("'/x y/codex'", { followUp: '/mcp' })
  })

  it('types OpenCode its own slash command, /mcps, after the launch', async () => {
    runMock.mockResolvedValue('typed')
    const wrapper = mountButton({
      connection: connection({ name: 'opencode', label: 'OpenCode', mcp_command: 'opencode', mcp_slash: '/mcps' }),
    })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith('opencode', { followUp: '/mcps' })
  })

  it('renders nothing when the row has no mcp_command', () => {
    const wrapper = mountButton({ connection: connection({ mcp_command: undefined }) })
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
  })

  it('stays silent when the command was typed', async () => {
    runMock.mockResolvedValue('typed')
    const wrapper = mountButton({ connection: connection() })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('shows one status line when the terminal was not ready and the command was copied', async () => {
    runMock.mockResolvedValue('copied')
    const wrapper = mountButton({ connection: connection() })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('The terminal was not ready, so the command was copied.')
    expect(wrapper.findAll('button')).toHaveLength(1)
  })

  it('shows a status line when nothing could reach the terminal or the clipboard', async () => {
    runMock.mockResolvedValue('failed')
    const wrapper = mountButton({ connection: connection() })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('Could not reach the terminal or the clipboard.')
  })
})
