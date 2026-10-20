// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'

const mockApiGet = vi.fn()

vi.mock('../../lib/api', () => ({
  api: { get: (p: string) => mockApiGet(p), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../../lib/hostOrigin', () => ({ isHostOrigin: () => false }))

const Stub = defineComponent({ render: () => h('div') })

beforeEach(() => {
  setActivePinia(createPinia())
  mockApiGet.mockResolvedValue({ configured: true, bootstrap: false, mode: 'configured', providers: {} })
})

describe('LoginView away from the host', () => {
  it('does not offer sign-in on this computer', async () => {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Stub }] })
    const mod = await import('../LoginView.vue')
    const wrapper = mount(mod.default as never, { global: { plugins: [router], stubs: { Teleport: true } } })
    await flushPromises()
    expect(wrapper.find('#login-token').exists()).toBe(true)
    expect(wrapper.find('.login-host-signin').exists()).toBe(false)
  })
})
