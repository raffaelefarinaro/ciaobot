// @vitest-environment jsdom

/**
 * `ChatTerminalEntry.test.ts` — the ways into the browser terminal.
 *
 * The terminal is not a repository feature. It opens a shell in a folder the
 * engine resolves, and a chat with no repository at all still needs one: a
 * provider sign-in, an install, an MCP server that stopped answering. So the
 * chat's Chat details carry a Terminal section of their own, and the project
 * page carries one of its own — both outside the repository, so exactly one
 * entry exists per surface and a project without a repository (including an
 * auto-managed one) is not left without a way in.
 *
 * The last case is the shell's own regression: a terminal asked for before
 * the chat page was mounted must still be mounted by it. The signal that
 * asks for the panel is asked for once and stays true, so a page mounted
 * afterwards has to read it, not wait for it to change.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia, type Pinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { defineComponent, h } from 'vue'
import { enableAutoUnmount, flushPromises, mount, shallowMount } from '@vue/test-utils'
import { api } from '../../lib/api'
import { terminalPanelWanted } from '../../lib/terminalPanel'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useTerminalStore } from '../../stores/terminal'
import type { ChatInfo, ProjectInfo } from '../../lib/types'
import ChatPanel from '../ChatPanel.vue'

// Every panel this file mounts is torn down when its test ends. A panel that
// outlives its test keeps its scroll-settle loop alive, and that loop calls
// `store.loadOlderMessages()` on a requestAnimationFrame. Store actions reset
// the one global "active pinia" the next `useProjectStore()` reads. A leftover
// panel could therefore hand the next test the previous test's store.
enableAutoUnmount(afterEach)

const ChildStub = defineComponent({
  name: 'ChildStub',
  setup() {
    return () => h('div')
  },
})

// The panel closes the comment popover on unmount, so the stub has to answer
// the call the panel makes.
const ChatCommentPopoverStub = defineComponent({
  name: 'ChatCommentPopoverStub',
  setup(_, { expose }) {
    expose({
      openId: null,
      close: vi.fn(),
      clearPendingClose: vi.fn(),
      onTargetOver: vi.fn(),
      onTargetOut: vi.fn(),
      pinFromEvent: vi.fn(),
      show: vi.fn(),
    })
    return () => h('div')
  },
})

const MODELS_RESPONSE = {
  models: ['haiku', 'sonnet', 'opus', 'fable'],
  default: 'sonnet',
  provider_models: { claude: ['haiku', 'sonnet', 'opus', 'fable'] },
  provider_defaults: { claude: 'sonnet' },
  thinking_levels: {},
  model_reasoning_levels: {},
  model_options: {},
  backends: { anthropic: true },
}

const REPO = { path: '/r/app', branch: 'main', dirty: false, missing: false, instructions: null }

function makeProject(overrides: Partial<ProjectInfo> = {}): ProjectInfo {
  return {
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
    context: '',
    created_at: '',
    order: 0,
    vault_folder: '',
    ...overrides,
  }
}

function makeChat(chatId: string): ChatInfo {
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: 'sonnet',
    provider: 'opencode',
    mode: 'normal',
    session_id: '',
    created_at: '',
    archived: false,
  }
}

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

/** A router with the routes the two surfaces link to. */
async function makeRouter(): Promise<Router> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: ChildStub },
      { path: '/chat/:chatId?', component: ChildStub },
      { path: '/project/:projectId', component: ChildStub },
      { path: '/schedules/:scheduleId', component: ChildStub },
    ],
  })
  await router.push('/')
  await router.isReady()
  return router
}

describe('the ways into the browser terminal', () => {
  let pinia: Pinia
  let router: Router
  // The panel settles its scroll position on unmount, and jsdom has no
  // `Element.scrollTo` to call.
  const scrollProto = Element.prototype as unknown as { scrollTo?: unknown }
  const originalScrollTo = scrollProto.scrollTo
  // jsdom has no ResizeObserver, and the rail is only rendered on a pane wide
  // enough for it (or as a drawer). A wide pane is the ordinary case.
  const originalObserver = globalThis.ResizeObserver

  beforeEach(async () => {
    pinia = createPinia()
    setActivePinia(pinia)
    router = await makeRouter()
    scrollProto.scrollTo = function scrollTo() {}
    ;(globalThis as { ResizeObserver?: unknown }).ResizeObserver = class {
      constructor(private readonly cb: ResizeObserverCallback) {}
      observe() {
        this.cb([{ contentRect: { width: 1200 } } as ResizeObserverEntry], this as unknown as ResizeObserver)
      }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      value: new MemoryStorage(),
    })
    localStorage.clear()
    // The signal is a module-level one-bit flag: a test that asked for the
    // terminal must not leave it set for the next one.
    terminalPanelWanted.value = false
    vi.spyOn(api, 'get').mockImplementation((path: string) => {
      if (path === '/api/models') return Promise.resolve(MODELS_RESPONSE) as never
      if (path.startsWith('/api/commands')) return Promise.resolve({ commands: [], skills: [] }) as never
      if (path === '/api/terminal/status') {
        return Promise.resolve({
          available: true,
          reason: '',
          platform: 'macos',
          host: 'engine.local',
          unlock_expires_in: 900,
          detached_ttl_seconds: 600,
        }) as never
      }
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] }) as never
      return Promise.resolve([]) as never
    })
  })

  afterEach(() => {
    // The store owns timers and a socket; it is disposed with the pinia it
    // belongs to, so the next test starts from a clean one.
    useTerminalStore(pinia).dispose()
    vi.restoreAllMocks()
    localStorage.clear()
    terminalPanelWanted.value = false
    scrollProto.scrollTo = originalScrollTo
    globalThis.ResizeObserver = originalObserver
  })

  /**
   * Mounts the chat panel on a pinia of its own and hands back that pinia's
   * stores. The stores come back with the wrapper on purpose: resolving them
   * again inside a test would read the global active pinia, which a panel
   * from an earlier test can replace.
   */
  async function mountChat(project: ProjectInfo) {
    const store = useProjectStore(pinia)
    store.projects = [project]
    store.chats = [makeChat('chat-1')]
    store.activeChatId = 'chat-1'
    store.messages = { 'chat-1': [] }
    store.bootstrapped = true
    vi.spyOn(useTaskStore(pinia), 'fetchSchedules').mockResolvedValue()

    const wrapper = shallowMount(ChatPanel, {
      global: {
        plugins: [pinia, router],
        stubs: {
          PaneHeader: ChildStub,
          ModelSelector: ChildStub,
          SubagentPanel: ChildStub,
          ChatCommentPopover: ChatCommentPopoverStub,
          CommentComposePopover: ChildStub,
          RouterLink: ChildStub,
          // The two components that carry the terminal are the real ones: the
          // point of the test is what they render.
          ChatRepoSection: false,
          BrowserTerminalButton: false,
        },
      },
    })
    await flushPromises()
    return { wrapper, terminal: useTerminalStore(pinia) }
  }

  /** Mounts the project page on the same pinia, for the project's own entry. */
  async function mountProject(project: ProjectInfo) {
    const store = useProjectStore(pinia)
    store.projects = [project] as unknown as typeof store.projects
    store.chats = [] as unknown as typeof store.chats
    store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
    store.projectStreaming = {}
    store.backgroundAgents = {}
    store.bootstrapped = true
    vi.spyOn(useTaskStore(pinia), 'fetchSchedules').mockResolvedValue()

    const { default: ProjectView } = await import('../ProjectView.vue')
    const wrapper = mount(ProjectView, {
      props: { projectId: 'project-1' },
      global: {
        plugins: [pinia, router],
        stubs: {
          PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' },
          BrowserTerminalButton: false,
        },
      },
    })
    await flushPromises()
    return wrapper
  }

  it('opens the terminal for a chat with no repository', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true } as never)
    const { wrapper, terminal } = await mountChat(makeProject())

    const section = wrapper.get('.chat-rail-terminal')
    // One row that is its own label: no separate "Terminal" heading above it.
    expect(section.find('.rail-label').exists()).toBe(false)
    const button = section.get('button')
    expect(button.text()).toContain('Open in Terminal')
    await button.trigger('click')
    await flushPromises()

    // The terminal is scoped to this chat, and the pane is asked for.
    expect(terminal.visible).toBe(true)
    expect(terminal.target).toEqual({ chatId: 'chat-1' })
    expect(terminalPanelWanted.value).toBe(true)
    // Opening the panel starts nothing: a shell is an explicit act.
    expect(post).not.toHaveBeenCalled()
  })

  it('gives a chat with a repository exactly one terminal entry', async () => {
    const { wrapper } = await mountChat(makeProject({ repo: REPO }))

    // The repository section carries the checkout facts, not a second way in.
    expect(wrapper.find('.chat-repo-section').exists()).toBe(true)
    expect(wrapper.find('.chat-repo-section').text()).not.toContain('Open in Terminal')
    const entries = wrapper.findAll('.chat-rail-terminal button')
    expect(entries).toHaveLength(1)
  })

  it('offers the terminal to a chat whose repository folder is missing', async () => {
    const { wrapper, terminal } = await mountChat(makeProject({ repo: { ...REPO, missing: true } }))

    // A missing folder is a repository fact, not a reason to lose the shell.
    expect(wrapper.get('.chat-repo-missing').text()).toContain('folder is missing')
    const button = wrapper.get('.chat-rail-terminal button')
    await button.trigger('click')
    await flushPromises()

    expect(terminal.visible).toBe(true)
    expect(terminal.target).toEqual({ chatId: 'chat-1' })
  })

  it('opens the terminal for a project with no repository', async () => {
    // The General project is the one every workspace has, and it has no
    // repository of its own unless one is linked.
    const wrapper = await mountProject(makeProject({ name: 'General' }))

    const section = wrapper.get('.project-terminal')
    const button = section.get('button')
    expect(button.text()).toBe('Open in Terminal')
    await button.trigger('click')
    await flushPromises()

    const terminal = useTerminalStore(pinia)
    expect(terminal.visible).toBe(true)
    expect(terminal.target).toEqual({ projectId: 'project-1' })
  })

  it('gives an auto project a terminal entry too', async () => {
    const wrapper = await mountProject(makeProject({ name: 'CLI project', is_auto: true }))

    // An auto project with no repository has no repository section at all —
    // and still has one way into the terminal.
    expect(wrapper.find('.project-repo').exists()).toBe(false)
    expect(wrapper.findAll('.project-terminal button')).toHaveLength(1)
  })

  it('gives a project with a repository exactly one terminal entry', async () => {
    const wrapper = await mountProject(makeProject({ repo: REPO }))

    // The repository section carries the checkout facts, not a second way in.
    expect(wrapper.find('.project-repo').exists()).toBe(true)
    expect(wrapper.get('.project-repo').text()).not.toContain('Open in Terminal')
    const entries = wrapper.findAll('button').filter(b => b.text() === 'Open in Terminal')
    expect(entries).toHaveLength(1)
  })

  it('mounts the terminal panel when the signal was already set', async () => {
    // The panel is asked for once and the signal stays true for the session.
    // A chat page mounted after that has to read the flag, not wait for it
    // to change — otherwise the terminal is invisible on the page that was
    // opened for it.
    terminalPanelWanted.value = true

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const store = useProjectStore(pinia)
    store.projects = [makeProject()] as unknown as typeof store.projects
    store.chats = [] as unknown as typeof store.chats
    store.bootstrapped = true
    vi.spyOn(store, 'fetchAll').mockResolvedValue()
    vi.spyOn(useTaskStore(pinia), 'fetchSchedules').mockResolvedValue()

    const mountLayout = () => mount(ChatLayout, {
      global: {
        plugins: [pinia, router],
        stubs: {
          ChatPanel: ChildStub,
          ProjectSidebar: ChildStub,
          ProjectView: ChildStub,
          SchedulePanel: ChildStub,
          SettingsView: ChildStub,
          FileViewerModal: ChildStub,
          PinnedFilePanel: ChildStub,
          PaneHeader: ChildStub,
          HomeRecentChats: ChildStub,
        },
      },
    })

    const wrapper = mountLayout()
    await flushPromises()
    // The pane is a chunk of its own: it arrives a tick after the shell.
    await vi.waitFor(() => {
      expect(wrapper.find('.terminal-pane').exists()).toBe(true)
    }, { timeout: 3000, interval: 20 })

    // The same page mounted again keeps the panel: the signal is not a
    // one-shot event, it is a state.
    wrapper.unmount()
    const remounted = mountLayout()
    await flushPromises()
    await vi.waitFor(() => {
      expect(remounted.find('.terminal-pane').exists()).toBe(true)
    }, { timeout: 3000, interval: 20 })
  })
})
