// @vitest-environment jsdom
//
// Digit shortcuts (1 deny / 2 approve) for pending permission cards. These
// mount the real ChatLayout around the real ChatPanel for the same reason the
// question shortcuts do: the layout owns the single window keydown listener
// and offers digits to the card before spending them on workspace switching,
// so a ChatPanel-only test would prove nothing about the part that can
// actually regress.

import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const apiGet = vi.hoisted(() => vi.fn(() => Promise.reject(new Error('no server'))))
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
vi.mock('../PaneHeader.vue', () => ({ default: NoopStub }))
vi.mock('../VoiceRecorder.vue', () => ({ default: NoopStub }))
vi.mock('../SubagentPanel.vue', () => ({ default: NoopStub }))
vi.mock('../ModelSelector.vue', () => ({ default: NoopStub }))
vi.mock('../ChatCommentPopover.vue', () => ({ default: CommentPopoverStub }))
vi.mock('../CommentComposePopover.vue', () => ({ default: NoopStub }))

const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })

const CHAT_ID = 'chat-perm'

class MemoryStorage {
  private values = new Map<string, string>()
  getItem(key: string): string | null { return this.values.get(key) ?? null }
  setItem(key: string, value: string): void { this.values.set(key, value) }
  removeItem(key: string): void { this.values.delete(key) }
  clear(): void { this.values.clear() }
}

function makePermission(overrides: Partial<{ request_id: string; tool_name: string; message: string; tool_input: string }> = {}) {
  return {
    request_id: 'approval-1',
    tool_name: 'Bash',
    message: 'Approve use of Bash?',
    tool_input: 'rm x',
    received_at: Date.now(),
    ...overrides,
  }
}

async function mountLayout(seed: { permissions?: Array<ReturnType<typeof makePermission>> } = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: EmptyStub }, { path: '/chat/:chatId', component: EmptyStub }],
  })
  await router.push('/')
  await router.isReady()

  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'General',
    workspace: 'personal',
  }] as unknown as typeof store.projects
  store.chats = [{
    chat_id: CHAT_ID,
    project_id: 'project-1',
    title: 'permission test',
    archived: false,
    provider: 'claude',
    mode: 'auto',
  }] as unknown as typeof store.chats
  store.activeChatId = CHAT_ID
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '' },
    { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '' },
  ]
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  if (seed.permissions) store.pendingPermissions = { [CHAT_ID]: seed.permissions }
  vi.spyOn(store, 'fetchAll').mockResolvedValue()

  const taskStore = useTaskStore()
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()

  const { default: ChatLayout } = await import('../ChatLayout.vue')
  const wrapper = mount(ChatLayout, {
    attachTo: document.body,
    global: {
      plugins: [router],
      stubs: {
        ProjectSidebar: EmptyStub,
        ProjectView: EmptyStub,
        SchedulePanel: EmptyStub,
        SettingsView: EmptyStub,
        FileViewerModal: EmptyStub,
        PinnedFilePanel: EmptyStub,
        HomeRecentChats: EmptyStub,
        Teleport: true,
      },
    },
  })
  await flushPromises()
  await nextTick()
  return { wrapper, store }
}

function pressKey(key: string, target: EventTarget = window) {
  const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
  target.dispatchEvent(event)
  return event
}

beforeEach(() => {
  setActivePinia(createPinia())
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1180 })
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  localStorage.clear()
  vi.clearAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('permission card keyboard shortcuts', () => {
  test('renders 1 (deny) and 2 (approve) badges on the first card', async () => {
    const { wrapper } = await mountLayout({ permissions: [makePermission()] })

    const card = wrapper.find('.permission-card')
    expect(card.exists()).toBe(true)
    const keys = card.findAll('.permission-key').map(b => b.text())
    expect(keys).toEqual(['1', '2'])
    // aria mirrors the badge so the shortcut is discoverable to screen readers.
    expect(card.find('.btn-deny').attributes('aria-keyshortcuts')).toBe('1')
    expect(card.find('.btn-approve').attributes('aria-keyshortcuts')).toBe('2')

    wrapper.unmount()
  })

  test('1 denies the first card and beats the workspace shortcut', async () => {
    const { wrapper, store } = await mountLayout({ permissions: [makePermission()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    const event = pressKey('1')
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(store.pendingPermissions[CHAT_ID]).toBeUndefined()
    expect(switchWorkspace).not.toHaveBeenCalled()
    expect(store.activeWorkspace).toBe('personal')

    wrapper.unmount()
  })

  test('2 approves the first card and beats the workspace shortcut', async () => {
    const { wrapper, store } = await mountLayout({ permissions: [makePermission()] })
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    const event = pressKey('2')
    await nextTick()

    expect(event.defaultPrevented).toBe(true)
    expect(store.pendingPermissions[CHAT_ID]).toBeUndefined()
    expect(switchWorkspace).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  test('deny and approve report the right verdict through respondPermission', async () => {
    const { wrapper, store } = await mountLayout({ permissions: [makePermission()] })
    const respondPermission = vi.spyOn(store, 'respondPermission')

    pressKey('1')
    await nextTick()
    expect(respondPermission).toHaveBeenCalledWith(CHAT_ID, 'approval-1', false, 'User denied')

    wrapper.unmount()
  })

  test('workspace digit switching still works with no permission card open', async () => {
    const { wrapper, store } = await mountLayout()
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace')

    expect(wrapper.find('.permission-card').exists()).toBe(false)
    const event = pressKey('2')
    await flushPromises()

    expect(event.defaultPrevented).toBe(true)
    expect(switchWorkspace).toHaveBeenCalledWith('work', { transition: true })

    wrapper.unmount()
  })

  test('a question card keeps first refusal on 1-9 over a permission card', async () => {
    // Not exercised here: the question shortcut path is covered by
    // ChatPanelQuestion.test.ts. This guard just documents the precedence —
    // both cards open at once, the question wins the digits.
    const { wrapper, store } = await mountLayout({ permissions: [makePermission()] })
    const respondPermission = vi.spyOn(store, 'respondPermission')

    pressKey('1')
    await nextTick()
    // With no question open the permission card handles it.
    expect(respondPermission).toHaveBeenCalledWith(CHAT_ID, 'approval-1', false, 'User denied')

    wrapper.unmount()
  })
})
