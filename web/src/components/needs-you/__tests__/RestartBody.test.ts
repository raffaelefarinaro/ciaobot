// @vitest-environment jsdom

// A `restart` card only navigates. Its button opens the Settings control that
// restarts or updates; it never calls the engine, so the confirmation and the
// drain warnings in Settings still apply.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import RestartBody from '../RestartBody.vue'
import { api } from '../../../lib/api'
import { useProjectStore } from '../../../stores/projects'
import type { ChatAction } from '../../../lib/types'

function restartAction(over: Partial<ChatAction> = {}): ChatAction {
  return {
    id: 'a1',
    kind: 'restart',
    origin: 'agent',
    state: 'open',
    params: { reason: 'config', note: 'Network access changed' },
    error: '',
    created_at: 1,
    ...over,
  }
}

describe('RestartBody', () => {
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

  it('opens This host on the Advanced tab and never calls the engine', async () => {
    const post = vi.spyOn(api, 'post')
    const startChatAction = vi.spyOn(useProjectStore(), 'startChatAction')

    wrapper = mount(RestartBody, {
      props: { chatId: 'c1', action: restartAction() },
      global: { plugins: [router] },
    })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/settings/advanced#restart-host')
    expect(post).not.toHaveBeenCalled()
    expect(startChatAction).not.toHaveBeenCalled()
  })
})
