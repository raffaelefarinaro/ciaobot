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
    expect(wrapper.find('#setup-vault').exists()).toBe(false)
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
    // one always-visible folder question: the second brain
    const vaultInput = wrapper.find('#setup-vault')
    expect(vaultInput.exists()).toBe(true)
    expect((vaultInput.element as HTMLInputElement).value).toBe('~/ciaobot-brain')
    expect(wrapper.find('#setup-vault-browse').exists()).toBe(true)
    // the app workspace is plumbing: hidden behind Advanced
    expect(wrapper.find('#setup-workspace').exists()).toBe(false)
    expect(wrapper.find('#setup-push').exists()).toBe(true)
    expect(wrapper.text()).toContain('ciao auth claude')

    // feature tour fills the 2-column grid: six tiles, no empty slot
    const tourItems = wrapper.findAll('.tour-list li')
    expect(tourItems.length).toBe(6)
    expect(tourItems[5].text()).toContain('Files, with history.')
    expect(tourItems[5].text()).toContain('Create, preview, edit, and restore workspace files right from the UI.')
  })

  it('hides the app data folder, port, and python inputs behind the Advanced toggle', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {}
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('#setup-workspace').exists()).toBe(false)
    expect(wrapper.find('#setup-port').exists()).toBe(false)
    expect(wrapper.find('#setup-python').exists()).toBe(false)

    await wrapper.find('#setup-advanced-toggle').trigger('click')
    await nextTick()
    expect(wrapper.find('#setup-workspace').exists()).toBe(true)
    expect(wrapper.find('#setup-workspace-browse').exists()).toBe(true)
    expect((wrapper.find('#setup-workspace').element as HTMLInputElement).value).toBe('~/.ciaobot')
    expect(wrapper.text()).toContain('App data folder — config, runtime state, chat metadata. Default: ~/.ciaobot')
    expect(wrapper.find('#setup-port').exists()).toBe(true)
    expect(wrapper.find('#setup-python').exists()).toBe(true)
    expect((wrapper.find('#setup-port').element as HTMLInputElement).value).toBe('8443')
  })

  it('switches the second-brain hint between scratch and existing modes', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {}
    })

    const wrapper = await mountLoginView()
    // scratch mode: same field, create-here hint
    expect(wrapper.find('#setup-vault').exists()).toBe(true)
    expect(wrapper.text()).toContain("We'll create your vault here")

    await wrapper.find('input[type="radio"][value="existing"]').setValue()
    await nextTick()
    // the field stays; only the hint changes
    expect(wrapper.find('#setup-vault').exists()).toBe(true)
    expect(wrapper.find('#setup-vault-browse').exists()).toBe(true)
    expect(wrapper.text()).toContain('Point at the notes folder you already have')
  })

  it('opens the folder picker, lists directories, and writes the selection into the second-brain field', async () => {
    const listing = {
      path: '/Users/me/ciaobot-brain',
      display_path: '~/ciaobot-brain',
      parent: '/Users/me',
      dirs: [
        { name: 'notes', path: '/Users/me/ciaobot-brain/notes' },
        { name: 'projects', path: '/Users/me/ciaobot-brain/projects' },
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

    await wrapper.find('#setup-vault-browse').trigger('click')
    await flushPromises()
    expect(mockApiGet).toHaveBeenCalledWith(
      `/api/setup/list-dirs?path=${encodeURIComponent('~/ciaobot-brain')}`
    )
    expect(wrapper.find('.picker-modal').exists()).toBe(true)
    expect(wrapper.find('.picker-path').text()).toBe('~/ciaobot-brain')
    const dirButtons = wrapper.findAll('.picker-dir')
    expect(dirButtons.map(b => b.text())).toEqual(['notes/', 'projects/'])

    await wrapper.find('.picker-select').trigger('click')
    await nextTick()
    expect(wrapper.find('.picker-modal').exists()).toBe(false)
    expect((wrapper.find('#setup-vault').element as HTMLInputElement).value).toBe('/Users/me/ciaobot-brain')
  })

  it('enables Finish without a push contact and wraps a plain email on submit', async () => {
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
    // push contact is optional: second-brain & app-data defaults + ready provider suffice
    expect(submitBtn.element.hasAttribute('disabled')).toBe(false)

    // a plain email is accepted and wrapped into a mailto: URI on submit
    const pushInput = wrapper.find('#setup-push')
    await pushInput.setValue('owner@example.com')
    await nextTick()

    // submit the form
    mockApiPost.mockResolvedValue({ ok: true })
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(mockApiPost).toHaveBeenCalledWith('/api/setup/finish', {
      workspace: '~/.ciaobot',
      vault_root: '~/ciaobot-brain',
      vault_mode: 'scratch',
      push_contact: 'mailto:owner@example.com',
      port: 8443,
      python: undefined,
      auth_required: true,
      restart: true,
    })
    expect(wrapper.text()).toContain('restarting')
  })

  it('strips mailto: for display and submits an empty push contact untouched', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: { name: 'claude', ok: true, auth: 'oauth', command: 'ciao auth claude', detail: 'Ready' }
      }
    })

    const wrapper = await mountLoginView()

    // pasting a mailto: URI shows as a plain email
    const pushInput = wrapper.find('#setup-push')
    await pushInput.setValue('mailto:owner@example.com')
    await nextTick()
    expect((pushInput.element as HTMLInputElement).value).toBe('owner@example.com')

    // clearing it submits an empty contact (push disabled until Settings)
    await pushInput.setValue('')
    await nextTick()
    mockApiPost.mockResolvedValue({ ok: true })
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    expect(mockApiPost).toHaveBeenCalledWith(
      '/api/setup/finish',
      expect.objectContaining({ push_contact: '' })
    )
  })
})
