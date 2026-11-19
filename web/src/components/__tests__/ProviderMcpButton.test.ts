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
    command: 'claude auth login',
    mcp_command: 'claude mcp',
    ...over,
  }
}

function mountButton(props: { connection: ProviderConnection; chatId?: string }) {
  return mount(ProviderMcpButton, { props })
}

describe('ProviderMcpButton', () => {
  afterEach(() => { runMock.mockReset() })

  it('renders the agent label and the server-built command', () => {
    const wrapper = mountButton({ connection: connection() })
    expect(wrapper.text()).toContain('Open MCP settings in Claude Code')
    expect(wrapper.find('code').text()).toBe('claude mcp')
  })

  it('types mcp_command into the in-app terminal, with the chat when one is given', async () => {
    runMock.mockResolvedValue('typed')
    const withChat = mountButton({ connection: connection(), chatId: 'c1' })
    await withChat.get('button').trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith('claude mcp', { chatId: 'c1' })

    runMock.mockClear()
    const projectScoped = mountButton({ connection: connection() })
    await projectScoped.get('button').trigger('click')
    await flushPromises()
    expect(runMock).toHaveBeenCalledWith('claude mcp', undefined)
  })

  it('renders nothing when the row has no mcp_command', () => {
    const wrapper = mountButton({ connection: connection({ mcp_command: undefined }) })
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toBe('')
  })

  it('shows the copy note when the terminal run is copied', async () => {
    runMock.mockResolvedValue('copied')
    const wrapper = mountButton({ connection: connection(), chatId: 'c1' })
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toContain('The terminal was not ready, so the command was copied.')
  })
})
