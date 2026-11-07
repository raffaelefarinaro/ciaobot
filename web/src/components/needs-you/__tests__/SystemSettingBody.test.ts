// @vitest-environment jsdom

// A `system_setting` card opens one macOS privacy pane on the engine's own Mac.
// Off the Mac's own page the card has no Open button and says where to go.
// Restart in Settings only navigates; it never calls the engine.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import SystemSettingBody from '../SystemSettingBody.vue'
import { isLoopbackPage } from '../../../lib/loopback'
import { api } from '../../../lib/api'
import { useProjectStore } from '../../../stores/projects'
import type { ChatAction } from '../../../lib/types'

vi.mock('../../../lib/loopback', () => ({ isLoopbackPage: vi.fn() }))

function settingAction(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'system_setting',
    origin: 'agent',
    state: 'open',
    params: { setting: 'camera' },
    error: '',
    created_at: 1,
    ...over,
  }
}

describe('SystemSettingBody', () => {
  let router: Router
  let wrapper: VueWrapper | null = null

  beforeEach(async () => {
    setActivePinia(createPinia())
    router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/:pathMatch(.*)*', component: defineComponent({ render: () => h('div') }) }],
    })
    await router.push('/')
    await router.isReady()
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    vi.restoreAllMocks()
  })

  it('opens the pane on the Mac through the store, with an empty body', async () => {
    vi.mocked(isLoopbackPage).mockReturnValue(true)
    const startChatAction = vi
      .spyOn(useProjectStore(), 'startChatAction')
      .mockResolvedValue(settingAction({ state: 'done' }))

    wrapper = mount(SystemSettingBody, {
      props: { chatId: 'c1', action: settingAction() },
      global: { plugins: [router] },
    })
    const open = wrapper.findAll('button').find(b => b.text() === 'Open System Settings')
    expect(open).toBeDefined()
    await open!.trigger('click')
    await flushPromises()

    expect(startChatAction).toHaveBeenCalledWith('c1', 'a1', {})
  })

  it('has no Open button off the Mac and says to open it there', () => {
    vi.mocked(isLoopbackPage).mockReturnValue(false)

    wrapper = mount(SystemSettingBody, {
      props: { chatId: 'c1', action: settingAction() },
      global: { plugins: [router] },
    })

    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.text()).toContain('Open this on your Mac')
    expect(wrapper.text()).toContain('Camera')
  })

  it('shows the not-local refusal as a notice, not an error', async () => {
    vi.mocked(isLoopbackPage).mockReturnValue(true)
    vi.spyOn(useProjectStore(), 'startChatAction').mockRejectedValue({ payload: { error: 'not_local' } })

    wrapper = mount(SystemSettingBody, {
      props: { chatId: 'c1', action: settingAction() },
      global: { plugins: [router] },
    })
    await wrapper.findAll('button')[0]!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('notice')).toContainEqual(['Open this on your Mac: System Settings → Privacy & Security.'])
    expect(wrapper.emitted('error')).not.toContainEqual(['System Settings could not be opened. Try again.'])
  })

  it('reports any other failure as an error', async () => {
    vi.mocked(isLoopbackPage).mockReturnValue(true)
    vi.spyOn(useProjectStore(), 'startChatAction').mockRejectedValue(new Error('x'))

    wrapper = mount(SystemSettingBody, {
      props: { chatId: 'c1', action: settingAction() },
      global: { plugins: [router] },
    })
    await wrapper.findAll('button')[0]!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('error')).toContainEqual(['System Settings could not be opened. Try again.'])
  })

  it('offers Restart in Settings for a done Full Disk Access card, and never calls the engine', async () => {
    vi.mocked(isLoopbackPage).mockReturnValue(true)
    const post = vi.spyOn(api, 'post')
    const startChatAction = vi.spyOn(useProjectStore(), 'startChatAction')

    wrapper = mount(SystemSettingBody, {
      props: {
        chatId: 'c1',
        action: settingAction({ state: 'done', params: { setting: 'full_disk_access' } }),
      },
      global: { plugins: [router] },
    })
    const restart = wrapper.findAll('button').find(b => b.text() === 'Restart Ciaobot in Settings')
    expect(restart).toBeDefined()
    await restart!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/settings/advanced#restart-host')
    expect(post).not.toHaveBeenCalled()
    expect(startChatAction).not.toHaveBeenCalled()
  })

  it('shows no extra button for a done card for another pane', () => {
    vi.mocked(isLoopbackPage).mockReturnValue(true)

    wrapper = mount(SystemSettingBody, {
      props: { chatId: 'c1', action: settingAction({ state: 'done' }) },
      global: { plugins: [router] },
    })

    expect(wrapper.find('button').exists()).toBe(false)
  })
})
