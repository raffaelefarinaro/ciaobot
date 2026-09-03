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
          command: 'claude auth login',
          detail: 'Run OAuth'
        }
      }
    })

    const wrapper = await mountLoginView()
    // The wizard, not the login prompt: it sets the password instead of asking
    // for an existing one.
    expect(wrapper.find('#login-token').exists()).toBe(false)
    expect(wrapper.find('#setup-password').exists()).toBe(true)
    expect(wrapper.find('#setup-workspace').exists()).toBe(true)
    expect(wrapper.find('#setup-workspace-browse').exists()).toBe(true)
    // scratch mode hides the vault input behind the derived-path hint
    expect(wrapper.find('#setup-vault').exists()).toBe(false)
    // No notification-email field: Web Push works out of the box with a
    // default VAPID subject, so setup never asks for a contact.
    expect(wrapper.find('#setup-push').exists()).toBe(false)
    expect(wrapper.text()).toContain('claude auth login')
    expect(wrapper.text()).toContain('Keep the terminal running ciao run open while you finish this setup')
    expect(wrapper.text()).toContain('close the terminal and open Ciaobot Server.app')

    // feature tour fills the 2-column grid: six tiles, no empty slot
    const tourItems = wrapper.findAll('.tour-list li')
    expect(tourItems.length).toBe(6)
    expect(tourItems[5].text()).toContain('Files, with history.')
    expect(tourItems[5].text()).toContain('Create, preview, edit, and restore workspace files right from the UI.')
  })

  it('hides port and python inputs behind the Advanced toggle', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {}
    })

    const wrapper = await mountLoginView()
    expect(wrapper.find('#setup-port').exists()).toBe(false)
    expect(wrapper.find('#setup-python').exists()).toBe(false)
    expect(wrapper.find('#setup-api-fallback').exists()).toBe(false)
    // The password is enforced, so it belongs in the main flow — never behind
    // Advanced, and there is no on/off toggle to hide there either.
    expect(wrapper.find('#setup-password').exists()).toBe(true)
    expect(wrapper.find('#setup-auth-required').exists()).toBe(false)

    await wrapper.find('#setup-advanced-toggle').trigger('click')
    await nextTick()
    expect(wrapper.find('#setup-port').exists()).toBe(true)
    expect(wrapper.find('#setup-python').exists()).toBe(true)
    expect(wrapper.find('#setup-api-fallback').exists()).toBe(true)
    expect(wrapper.find('#setup-auth-required').exists()).toBe(false)
    expect((wrapper.find('#setup-port').element as HTMLInputElement).value).toBe('8443')
  })

  it('asks only for the workspace folder and explains the auto-adjust behavior', async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/setup/inspect-folder')) {
        // Empty folder: no nested workspaces, fall through to the text field.
        return Promise.resolve({ mode: 'scratch', existing_workspaces: [] })
      }
      return Promise.resolve({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {}
      })
    })

    const wrapper = await mountLoginView()
    // single-folder setup: one path input, no vault input, no mode radio —
    // the server detects empty vs existing-notes folders on finish.
    expect(wrapper.find('#setup-vault').exists()).toBe(false)
    expect(wrapper.find('input[type="radio"][value="existing"]').exists()).toBe(false)
    expect(wrapper.find('input[type="radio"][value="scratch"]').exists()).toBe(false)
    expect(wrapper.text()).toContain("detects what's inside and adjusts automatically")
    // The logical workspace name is chosen here, defaulting to "personal".
    const nameInput = wrapper.find('#setup-workspace-name')
    expect(nameInput.exists()).toBe(true)
    expect((nameInput.element as HTMLInputElement).value).toBe('personal')
    expect(wrapper.text()).toContain('Logical Workspace Name')
  })

  it('hides the "First Workspace" field and shows chips when the folder already has nested workspaces', async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/setup/inspect-folder')) {
        return Promise.resolve({
          mode: 'existing',
          existing_workspaces: ['personal', 'work'],
        })
      }
      return Promise.resolve({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {}
      })
    })

    const wrapper = await mountLoginView()
    // The text field is gone; the detected names show as chips instead.
    expect(wrapper.find('#setup-workspace-name').exists()).toBe(false)
    const chips = wrapper.findAll('.workspace-chip')
    expect(chips.map(c => c.text())).toEqual(['personal', 'work'])
    expect(wrapper.text()).toContain('Found 2 workspaces')
  })

  it('omits workspace_name from the finish payload when the folder already has nested workspaces', async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/setup/inspect-folder')) {
        return Promise.resolve({
          mode: 'existing',
          existing_workspaces: ['personal', 'work'],
        })
      }
      return Promise.resolve({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {
          claude: { name: 'claude', ok: true, auth: 'oauth', command: 'claude auth login', detail: 'Ready' }
        }
      })
    })

    const wrapper = await mountLoginView()
    mockApiPost.mockResolvedValue({ ok: true })
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    const body = mockApiPost.mock.calls[0][1]
    expect(body.workspace).toBe('~/ciaobot')
    // Server adopts the discovered workspaces via detect_nested_workspaces;
    // we don't send a synthetic name that would otherwise be ignored.
    expect(body).not.toHaveProperty('workspace_name')
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
  })

  it('steps into a folder it just created so the confirm button names it', async () => {
    const home = {
      path: '/Users/me',
      display_path: '~',
      parent: '/Users',
      dirs: [] as Array<{ name: string; path: string }>,
      home: '/Users/me',
    }
    const created = {
      path: '/Users/me/ciaobot',
      display_path: '~/ciaobot',
      parent: '/Users/me',
      dirs: [],
      home: '/Users/me',
    }
    mockApiGet.mockImplementation((path: string) => {
      if (path === `/api/setup/list-dirs?path=${encodeURIComponent('/Users/me/ciaobot')}`) {
        return Promise.resolve(created)
      }
      if (path.startsWith('/api/setup/list-dirs')) return Promise.resolve(home)
      return Promise.resolve({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {},
      })
    })
    // The server answers mkdir with the *parent* listing, which is what used to
    // leave the picker pointing at the home directory.
    mockApiPost.mockImplementation((path: string) => {
      if (path === '/api/setup/mkdir') {
        return Promise.resolve({
          ...home,
          dirs: [{ name: 'ciaobot', path: '/Users/me/ciaobot' }],
        })
      }
      return Promise.resolve({})
    })

    const wrapper = await mountLoginView()
    await wrapper.find('#setup-workspace-browse').trigger('click')
    await flushPromises()

    await wrapper.find('.picker-new input').setValue('ciaobot')
    await wrapper.find('.picker-new button').trigger('click')
    await flushPromises()

    expect(wrapper.find('.picker-path').text()).toBe('~/ciaobot')
    expect(wrapper.find('.picker-select').text()).toContain('ciaobot')

    await wrapper.find('.picker-select').trigger('click')
    await nextTick()
    expect((wrapper.find('#setup-workspace').element as HTMLInputElement).value).toBe(
      '/Users/me/ciaobot',
    )
  })

  it('asks for an install, with a docs link, when the provider CLI is missing', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'not_installed',
          command: 'curl -fsSL https://claude.ai/install.sh | bash',
          detail: 'Claude Code is not installed on this machine.',
          install_url: 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code',
          path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc',
        },
      },
    })

    const wrapper = await mountLoginView()

    expect(wrapper.text()).toContain('[!] Not Installed')
    expect(wrapper.text()).toContain('curl -fsSL https://claude.ai/install.sh | bash')
    expect(wrapper.text()).toContain('Not installed yet.')
    const link = wrapper.find('.install-link')
    expect(link.attributes('href')).toBe(
      'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code',
    )
  })

  it('offers the PATH line as a second copyable step after the install', async () => {
    // Claude's installer drops the binary in ~/.local/bin, which a default
    // macOS PATH omits: without this step the next command a new user runs
    // still reports "command not found".
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'not_installed',
          command: 'curl -fsSL https://claude.ai/install.sh | bash',
          detail: 'Claude Code is not installed on this machine.',
          path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc',
        },
      },
    })

    const wrapper = await mountLoginView()

    const rows = wrapper.findAll('.command-row')
    expect(rows).toHaveLength(2)
    expect(rows[1].text()).toContain('export PATH="$HOME/.local/bin:$PATH"')
    expect(wrapper.text()).toContain('Then make it findable in new terminals:')
  })

  it('flashes Copied! on the button that was pressed, not on both', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'not_installed',
          command: 'curl -fsSL https://claude.ai/install.sh | bash',
          detail: 'Claude Code is not installed on this machine.',
          path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc',
        },
      },
    })

    const wrapper = await mountLoginView()
    const buttons = wrapper.findAll('.command-row button')
    await buttons[1].trigger('click')
    await flushPromises()

    expect(writeText).toHaveBeenCalledWith(
      'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc && source ~/.zshrc',
    )
    expect(wrapper.findAll('.command-row button')[0].text()).toBe('Copy')
    expect(wrapper.findAll('.command-row button')[1].text()).toBe('Copied!')
  })

  it('does not let an earlier copy timer clear the button just pressed', async () => {
    vi.useFakeTimers()
    try {
      const writeText = vi.fn().mockResolvedValue(undefined)
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText },
        configurable: true,
      })
      mockApiGet.mockResolvedValue({
        configured: false,
        bootstrap: true,
        mode: 'bootstrap',
        providers: {
          claude: {
            name: 'claude',
            ok: false,
            auth: 'not_installed',
            command: 'curl -fsSL https://claude.ai/install.sh | bash',
            detail: 'Claude Code is not installed on this machine.',
            path_command: 'echo \'export PATH="$HOME/.local/bin:$PATH"\' >> ~/.zshrc',
          },
        },
      })

      const wrapper = await mountLoginView()
      await wrapper.findAll('.command-row button')[0].trigger('click')
      await flushPromises()
      vi.advanceTimersByTime(1500)
      await wrapper.findAll('.command-row button')[1].trigger('click')
      await flushPromises()
      vi.advanceTimersByTime(600)
      await nextTick()

      // The first press's revert must not fire while the second is showing.
      expect(wrapper.findAll('.command-row button')[1].text()).toBe('Copied!')
    } finally {
      vi.useRealTimers()
    }
  })

  it('shows no PATH step when the provider CLI is already installed', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'missing',
          command: 'claude auth login',
          detail: 'Run Claude OAuth or set ANTHROPIC_API_KEY.',
        },
      },
    })

    const wrapper = await mountLoginView()

    expect(wrapper.findAll('.command-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('claude auth login')
    expect(wrapper.text()).not.toContain('Then make it findable')
  })

  it('points at the desktop app when only the CLI is missing', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: false,
          auth: 'not_installed',
          command: 'curl -fsSL https://claude.ai/install.sh | bash',
          detail: 'The Claude desktop app is installed (/Applications/Claude.app), but…',
          install_url: 'https://code.claude.com/docs/en/quickstart#step-1-install-claude-code',
          app_path: '/Applications/Claude.app',
        },
      },
    })

    const wrapper = await mountLoginView()

    expect(wrapper.text()).toContain('The desktop app is installed, but Ciaobot drives the CLI.')
  })

  it('finishes setup without collecting a notification email', async () => {
    mockApiGet.mockResolvedValue({
      configured: false,
      bootstrap: true,
      mode: 'bootstrap',
      providers: {
        claude: {
          name: 'claude',
          ok: true,
          auth: 'oauth',
          command: 'claude auth login',
          detail: 'Ready'
        }
      }
    })

    const wrapper = await mountLoginView()
    // No push-contact field is shown anymore.
    expect(wrapper.find('#setup-push').exists()).toBe(false)

    const submitBtn = wrapper.find('button[type="submit"]')
    // A password is mandatory: the wizard cannot be submitted without one.
    expect(submitBtn.element.hasAttribute('disabled')).toBe(true)
    await wrapper.find('#setup-password').setValue('hunter2')
    await wrapper.find('#setup-password-confirm').setValue('hunter2')
    await nextTick()
    expect(submitBtn.element.hasAttribute('disabled')).toBe(false)

    mockApiPost.mockResolvedValue({ ok: true })
    await wrapper.find('form').trigger('submit.prevent')
    await flushPromises()

    // Web Push uses a default VAPID subject server-side, so setup never sends
    // a push_contact.
    expect(mockApiPost).toHaveBeenCalledWith('/api/setup/finish', {
      workspace: '~/ciaobot',
      workspace_name: 'personal',
      port: 8443,
      python: undefined,
      password: 'hunter2',
      provider: 'claude',
      restart: true,
    })
    const payload = mockApiPost.mock.calls[0][1]
    expect(payload).not.toHaveProperty('push_contact')
    expect(wrapper.text()).toContain('restarting')
  })

  it('shows switch-back-to-host bailout in client login mode', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/startup-status')) {
        return {
          ok: true,
          json: async () => ({
            node_role: 'client',
            host_url: 'http://100.101.252.27:8443',
            has_host_session: false,
          }),
        } as Response
      }
      return { ok: false, json: async () => ({}) } as Response
    })
    vi.stubGlobal('fetch', fetchMock)

    mockApiGet.mockResolvedValue({
      configured: true,
      bootstrap: false,
      mode: 'configured',
      providers: {},
    })

    const wrapper = await mountLoginView()
    expect(wrapper.text()).toContain('host password required')
    expect(wrapper.text()).toContain('Switch back to host')

    mockApiPost.mockResolvedValue({ ok: true, status: { role: 'host' } })
    const assign = vi.fn()
    // LoginView asks through lib/confirm now, not window.confirm.
    const confirmModule = await import('../../lib/confirm')
    const askConfirmSpy = vi.spyOn(confirmModule, 'askConfirm').mockResolvedValue(true)
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign, pathname: '/login', href: 'http://localhost/login' },
    })

    await wrapper.find('.client-bailout-btn').trigger('click')
    await flushPromises()

    expect(mockApiPost).toHaveBeenCalledWith('/api/node/handover', {
      target_node_url: 'http://100.101.252.27:8443',
      force: true,
    })
    expect(assign).toHaveBeenCalledWith('/')
    askConfirmSpy.mockRestore()  // clearAllMocks does not undo a spy
    vi.unstubAllGlobals()
  })
})
