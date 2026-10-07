// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { api } from '../../lib/api'
import ProviderSignInHint from '../ProviderSignInHint.vue'

async function open(wrapper: ReturnType<typeof mount>) {
  const details = wrapper.find('details').element as HTMLDetailsElement
  details.open = true
  await wrapper.find('details').trigger('toggle')
  await flushPromises()
}

describe('ProviderSignInHint', () => {
  afterEach(() => vi.restoreAllMocks())

  it('shows the engine-reported command for the chat provider, fetched on open', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      connections: {
        claude: { name: 'claude', label: 'Claude Code', ok: false, auth: 'missing', command: "'/Applications/Ciaobot.app/claude' auth login" },
        opencode: { name: 'opencode', ok: true, auth: 'oauth', command: 'opencode auth login' },
      },
    } as never)
    const wrapper = mount(ProviderSignInHint, { props: { provider: 'claude', actionLabel: 'Try now' } })
    expect(get).not.toHaveBeenCalled()

    await open(wrapper)
    expect(get).toHaveBeenCalledWith('/api/settings/providers')
    expect(wrapper.find('code').text()).toBe("'/Applications/Ciaobot.app/claude' auth login")
    expect(wrapper.text()).toContain('to Claude Code')
    expect(wrapper.text()).toContain('click Try now')

    await wrapper.find('.signin-hint-action').trigger('click')
    expect(wrapper.emitted('action')).toHaveLength(1)
  })

  it('copies the command', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      connections: { claude: { name: 'claude', ok: false, auth: 'missing', command: 'claude auth login' } },
    } as never)
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    const wrapper = mount(ProviderSignInHint, { props: { provider: 'claude', actionLabel: 'Retry' } })
    await open(wrapper)

    const copyBtn = wrapper.findAll('button').find(b => b.text() === 'Copy')!
    await copyBtn.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('claude auth login')
    expect(copyBtn.text()).toBe('Copied')
  })

  it('points at Settings when the command cannot be looked up', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'))
    const wrapper = mount(ProviderSignInHint, { props: { provider: 'claude', actionLabel: 'Retry' } })
    await open(wrapper)
    expect(wrapper.find('[role="alert"]').text()).toContain('offline')
    expect(wrapper.find('[role="alert"]').text()).toContain('Settings → Models')
    expect(wrapper.find('code').exists()).toBe(false)
  })
})
