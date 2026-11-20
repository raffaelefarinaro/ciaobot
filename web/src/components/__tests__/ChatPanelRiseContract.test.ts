// @vitest-environment jsdom
//
// #3086 contract on the production ChatPanel, not a copy of it. The arrival
// class is owned by the receiving instance: the panel's own root carries
// `chat-rise` only when the layout's `riseForInstance` is that instance's token.
// The token comes from the panel's own `pane-mounted` event, so these tests
// observe the real hooks and the real root binding. A same-id replacement must
// never carry the class in any render or class write, including the first.

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { recordClassWrites } from '../../lib/testing/classWrites'

const apiGet = vi.hoisted(() => vi.fn((_path: string): Promise<unknown> => Promise.reject(new Error('no server'))))
const apiPost = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiPatch = vi.hoisted(() => vi.fn(() => Promise.resolve({})))
const apiDel = vi.hoisted(() => vi.fn(() => Promise.resolve({})))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

vi.mock('../../router', () => ({
  router: { push: vi.fn(), currentRoute: { value: { params: {} } } },
}))

const NoopStub = vi.hoisted(() => ({ name: 'NoopStub', render: () => null }))
const CommentPopoverStub = vi.hoisted(() => ({
  name: 'CommentPopoverStub',
  render: () => null,
  methods: {
    close: () => {},
    clearPendingClose: () => {},
    onTargetOver: () => {},
    onTargetOut: () => {},
    pinFromEvent: () => null,
    show: () => {},
  },
}))
vi.mock('../ModelSelector.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })
const CHAT_ID = 'chat-rise'

type PaneIdentity = { chatId: string; instance: symbol; beside: boolean }

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

async function seedStore() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId', component: EmptyStub }],
  })
  await router.push(`/chat/${CHAT_ID}`)
  await router.isReady()
  const store = useProjectStore()
  store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
  store.chats = [{
    chat_id: CHAT_ID, project_id: 'project-1', title: 'rise contract', archived: false,
    provider: 'claude', mode: 'bypass',
  }] as unknown as typeof store.chats
  store.activeChatId = CHAT_ID
  store.workspaces = [{ name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' }]
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  vi.spyOn(store, 'fetchAll').mockResolvedValue()
  vi.spyOn(useTaskStore(), 'fetchSchedules').mockResolvedValue()
  return { router }
}

/** Mounts the production ChatPanel for CHAT_ID with the given layout props. */
async function mountPanel(router: ReturnType<typeof createRouter>, props: { beside?: boolean; riseForInstance?: symbol | null }) {
  const { default: ChatPanel } = await import('../ChatPanel.vue')
  return mount(ChatPanel, {
    attachTo: document.body,
    props: { chatId: CHAT_ID, ...props },
    global: {
      plugins: [router],
      stubs: { Teleport: true, ModelSelector: NoopStub },
    },
  })
}

/** The token the panel announced on its own mount. */
function announcedIdentity(wrapper: { emitted: (name: string) => unknown[][] | undefined }): PaneIdentity {
  const [pane] = wrapper.emitted('pane-mounted') as Array<[PaneIdentity]>
  return pane[0]
}

const rootOf = (wrapper: { element: Element }) => wrapper.element as HTMLElement

describe('production ChatPanel: arrival class is owned by its instance (#3086)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
    localStorage.clear()
    vi.clearAllMocks()
    apiGet.mockImplementation(() => Promise.reject(new Error('no server')))
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  test('announces its own identity on mount, and the class follows its own token only', async () => {
    const { router } = await seedStore()
    const panel = await mountPanel(router, { riseForInstance: null })
    await flushPromises()
    const identity = announcedIdentity(panel)
    expect(identity.chatId).toBe(CHAT_ID)
    expect(identity.beside).toBe(false)
    expect(typeof identity.instance).toBe('symbol')
    expect(rootOf(panel).classList.contains('chat-rise')).toBe(false)

    await panel.setProps({ riseForInstance: identity.instance })
    expect(rootOf(panel).classList.contains('chat-rise')).toBe(true)
    await panel.setProps({ riseForInstance: Symbol('someone else') })
    expect(rootOf(panel).classList.contains('chat-rise')).toBe(false)
    panel.unmount()
  })

  test('a beside pane never carries the arrival class, even for its own token', async () => {
    const { router } = await seedStore()
    const beside = await mountPanel(router, { beside: true, riseForInstance: null })
    await flushPromises()
    const identity = announcedIdentity(beside)
    expect(identity.beside).toBe(true)
    await beside.setProps({ riseForInstance: identity.instance })
    expect(rootOf(beside).classList.contains('chat-rise')).toBe(false)
    beside.unmount()
  })

  test('a same-id replacement never carries the class in any render or class write', async () => {
    const { router } = await seedStore()
    const first = await mountPanel(router, { riseForInstance: null })
    await flushPromises()
    const token1 = announcedIdentity(first).instance
    // The layout starts token1's rise.
    await first.setProps({ riseForInstance: token1 })
    expect(rootOf(first).classList.contains('chat-rise')).toBe(true)

    // A same-id replacement is created while the layout still names token1,
    // before token1's unmount cleanup has run: its first VNode carries token1.
    const log = recordClassWrites()
    try {
      const second = await mountPanel(router, { riseForInstance: token1 })
      await flushPromises()
      expect(second.emitted('pane-mounted')).toHaveLength(1)
      expect(announcedIdentity(second).instance).not.toBe(token1)
      expect(rootOf(second).classList.contains('chat-rise')).toBe(false)
      expect(log.writes.some(w => w.value.includes('chat-rise'))).toBe(false)
      second.unmount()
    } finally {
      log.restore()
    }
    // token1's own unmount cleanup then ends its rise without touching anything else.
    first.unmount()
  })
})
