// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import SettingsView from '../SettingsView.vue'
import * as confirmation from '../../lib/confirm'

afterEach(() => vi.restoreAllMocks())

it.each([true, false])('chooses the advertised restart action (restart_only=%s)', async (restartOnly) => {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/settings', component: stub }] })
  await router.push('/settings')
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (path) => {
    if (path === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false, restart_only: restartOnly } as never
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, steps: [] })
  const confirm = vi.spyOn(confirmation, 'askConfirm').mockResolvedValue(true)
  const beginRestart = vi.spyOn(useProjectStore(), 'beginServerRestart').mockImplementation(() => {})
  const wrapper = mount(SettingsView, { global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } } })
  try {
    await flushPromises()
    await wrapper.findAll('button').find(b => b.text() === 'Restart')!.trigger('click')
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining(restartOnly ? 'Active chats will finish' : 'pull latest, rebuild'), expect.any(Object))
    expect(post).toHaveBeenCalledWith(restartOnly ? '/api/admin/restart' : '/api/admin/deploy', { confirm_warnings: false })
    expect(beginRestart).toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})
