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
    expect(wrapper.find('#setup-workspace-browse').exists()).toBe(true)
    // scratch mode hides the vault input behind the derived-path hint
    expect(wrapper.find('#setup-vault').exists()).toBe(false)
    expect(wrapper.find('#setup-push').exists()).toBe(true)
    expect(wrapper.text()).toContain('ciao auth claude')
  })

  it('shows a live derived vault hint in scratch mode and reveals the input via change location', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {}
    })

    const wrapper = await mountLoginView()
    // hidden by default, hint shows the derived path
    expect(wrapper.find('#setup-vault').exists()).toBe(false)
    expect(wrapper.text()).toContain('Your second-brain vault will be created at')
    expect(wrapper.text()).toContain('~/ciaobot/memory-vault')

    // hint live-updates when the workspace changes
    await wrapper.find('#setup-workspace').setValue('/tmp/space')
    await nextTick()
    expect(wrapper.text()).toContain('/tmp/space/memory-vault')

    // "change location" reveals the vault input with its Browse button
    await wrapper.find('#setup-vault-change').trigger('click')
    await nextTick()
    const vaultInput = wrapper.find('#setup-vault')
    expect(vaultInput.exists()).toBe(true)
    expect(wrapper.find('#setup-vault-browse').exists()).toBe(true)
    expect((vaultInput.element as HTMLInputElement).value).toBe('/tmp/space/memory-vault')
  })

  it('shows the vault input when vault mode is existing', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {}
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('#setup-vault').exists()).toBe(false)

    await wrapper.find('input[type="radio"][value="existing"]').setValue()
    await nextTick()
    expect(wrapper.find('#setup-vault').exists()).toBe(true)
    expect(wrapper.find('#setup-vault-browse').exists()).toBe(true)
    expect(wrapper.text()).toContain('Existing Notes Folder')
  })

  it('opens the folder picker, lists directories, and writes the selection into the workspace field', async () => {
    const listing = {
      path: '/Users/me/ciaobot',
      display_path: '~/ciaobot',
      parent: '/Users/me',
      dirs: [
        { name: 'memory-vault', path: '/Users/me/ciaobot/memory-vault' },
        { name: 'projects', path: '/Users/me/ciaobot/projects' },
      ],
      home: '/Users/me',
    }
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/setup/list-dirs')) return Promise.resolve(listing)
      return Promise.resolve({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {}
      })
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('.picker-modal').exists()).toBe(false)

    await wrapper.find('#setup-workspace-browse').trigger('click')
    await flushPromises()
    expect(mockApiGet).toHaveBeenCalledWith(
      `/api/setup/list-dirs?path=${encodeURIComponent('~/ciaobot')}`
    )
    expect(wrapper.find('.picker-modal').exists()).toBe(true)
    expect(wrapper.find('.picker-path').text()).toBe('~/ciaobot')
    const dirButtons = wrapper.findAll('.picker-dir')
    expect(dirButtons.map(b => b.text())).toEqual(['memory-vault/', 'projects/'])

    await wrapper.find('.picker-select').trigger('click')
    await nextTick()
    expect(wrapper.find('.picker-modal').exists()).toBe(false)
    expect((wrapper.find('#setup-workspace').element as HTMLInputElement).value).toBe('/Users/me/ciaobot')
    // workspace selection re-derives the hidden vault suggestion
    expect(wrapper.text()).toContain('/Users/me/ciaobot/memory-vault')
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
