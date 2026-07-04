// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { config, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'

const mockApiGet = vi.fn()
const mockApiPost = vi.fn()

vi.mock('../../lib/api', () => ({
  api: {
    get: (path: string) => mockApiGet(path),
    post: (path: string, body?: unknown) => mockApiPost(path, body),
    patch: vi.fn(() => Promise.resolve({})),
    del: vi.fn(() => Promise.resolve({})),
  }
}))

vi.mock('../../lib/push', () => ({
  pushSupported: () => false,
  pushEnabled: () => false,
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}))

const Stub = defineComponent({ render: () => h('div') })

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/login', name: 'login', component: Stub },
    ],
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  config.global.stubs = {
    Teleport: true,
  }
  mockApiGet.mockReset()
  mockApiPost.mockReset()
})

afterEach(() => {
  vi.clearAllMocks()
})

async function mountLoginView() {
  const router = makeRouter()
  const mod = await import('../LoginView.vue')
  const wrapper = mount(mod.default as never, {
    global: {
      plugins: [router],
    },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('LoginView setup wizard tests', () => {
  it('renders standard login when bootstrap is false', async () => {
    mockApiGet.mockResolvedValue({
      configured: true,
      bootstrap: false,
      mode: 'configured',
      providers: {}
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('input[type="password"]').exists()).toBe(true)
    expect(wrapper.find('#setup-workspace').exists()).toBe(false)
  })

  it('renders setup wizard when bootstrap is true', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      workspace_root: '/path',
      vault_root: '/path/memory-vault',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'missing',
          command: 'ciao auth claude',
          detail: 'Run OAuth'
        }
      }
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.find('#setup-workspace').exists()).toBe(true)
    expect(wrapper.find('#setup-vault').exists()).toBe(true)
    expect(wrapper.find('#setup-push').exists()).toBe(true)
    expect(wrapper.text()).toContain('ciao auth claude')
  })

  it('validates required fields and enables Finish on valid form', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: true,
          auth: 'oauth',
          command: 'ciao auth claude',
          detail: 'Ready'
        }
      }
    })

    const wrapper = await mountLoginView()
    const submitBtn = wrapper.find('button[type="submit"]')
    expect(submitBtn.element.hasAttribute('disabled')).toBe(true)

    // fill push contact
    const pushInput = wrapper.find('#setup-push')
    await pushInput.setValue('mailto:owner@example.com')
    await nextTick()

    // should be enabled now since workspace & vault default are filled, provider is ok
    expect(submitBtn.element.hasAttribute('disabled')).toBe(false)

    // submit the form
    mockApiPost.mockResolvedValue({ ok: true })
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(mockApiPost).toHaveBeenCalledWith('/api/setup/finish', {
      workspace: '~/ciaobot',
      vault_root: '~/ciaobot/memory-vault',
      vault_mode: 'scratch',
      push_contact: 'mailto:owner@example.com',
      port: 8443,
      python: undefined,
      auth_required: true,
      restart: true,
    })
    expect(wrapper.text()).toContain('restarting')
  })
})
