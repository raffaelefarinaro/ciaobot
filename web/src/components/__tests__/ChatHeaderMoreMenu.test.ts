// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import ChatHeaderMoreMenu from '../ChatHeaderMoreMenu.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useTaskStore } from '../../stores/tasks'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useNotificationInboxStore } from '../../stores/notificationInbox'
import { api } from '../../lib/api'
import { readyHealth } from '../../lib/sourceHealth'

const nav = vi.hoisted(() => ({
  canBack: { value: false },
  backLabel: { value: 'Back to Home' },
  back: vi.fn(),
}))
vi.mock('../../composables/useHistoryNav', () => ({ useHistoryNav: () => nav }))

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Empty = defineComponent({ template: '<div />' })

function chat(id: string) {
  return {
    chat_id: id,
    project_id: 'p1',
    title: `Chat ${id}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:30:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
  }
}

// One unread chat is one notification: the same seeding the bell's own tests use.
function seedUnread(count: number) {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  projects.projects = [{ project_id: 'p1', name: 'General', workspace: 'personal' }] as never
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = Array.from({ length: count }, (_, i) => chat(`c${i}`)) as never
  projects.chatsHealth = readyHealth()
  taskSignals.healthWorkspace = 'personal'
  taskSignals.loadedWorkspace = 'personal'
  taskSignals.health = readyHealth()
  const housekeeping = useHousekeepingStore()
  housekeeping.actionsHealth = readyHealth()
  housekeeping.updateTasksWorkspace = 'personal'
  housekeeping.updateTasksHealth = readyHealth()
  useTaskStore().schedulesLoaded = true
  useProposalsStore().loaded = true
  useVaultReviewStore().loadedWorkspace = 'personal'
  vi.mocked(api.get).mockReset()
  vi.mocked(api.get).mockImplementation(async (url: string) => {
    if (url.startsWith('/api/chats')) return projects.chats
    if (url.startsWith('/api/tasks')) return { workspace: 'personal', tasks: [] }
    return []
  })
}

let wrapper: VueWrapper | null = null

async function mountMenu(opts: { canArchive?: boolean; unread?: number; mcpProblems?: number } = {}) {
  setActivePinia(createPinia())
  seedUnread(opts.unread ?? 0)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/chat/:chatId', component: Empty }, { path: '/', component: Empty }],
  })
  await router.push('/')
  wrapper = mount(ChatHeaderMoreMenu, {
    props: { canArchive: opts.canArchive ?? true, mcpProblems: opts.mcpProblems ?? 0 },
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  return { wrapper, router }
}

const trigger = () => wrapper!.get<HTMLButtonElement>('button.chat-more-btn').element
const items = () => Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'))
const itemNamed = (name: string) => items().find(el => el.textContent?.trim().startsWith(name))
const sheet = () => document.body.querySelector('[role="dialog"][aria-label="Notifications"]')

async function openMenu() {
  trigger().click()
  await flushPromises()
  await nextTick()
}

async function settle() {
  await flushPromises()
  await new Promise(resolve => setTimeout(resolve, 0))
  await flushPromises()
}

describe('ChatHeaderMoreMenu', () => {
  beforeEach(() => {
    nav.canBack.value = false
    nav.back.mockReset()
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('lists Chat details, Notifications and Archive, and Back only with history', async () => {
    await mountMenu({ canArchive: true })
    await openMenu()
    expect(items().map(el => el.textContent?.trim())).toEqual(['Chat details', 'Notifications', 'Archive'])
    wrapper!.unmount()
    wrapper = null
    document.body.innerHTML = ''

    nav.canBack.value = true
    await mountMenu({ canArchive: false })
    await openMenu()
    expect(items().map(el => el.textContent?.trim())).toEqual(['Chat details', 'Notifications', 'Back to Home'])
  })

  it('emits details and archive', async () => {
    const { wrapper: w } = await mountMenu({ canArchive: true })
    await openMenu()
    itemNamed('Chat details')!.click()
    await settle()
    expect(w.emitted('details')).toHaveLength(1)

    await openMenu()
    itemNamed('Archive')!.click()
    await settle()
    expect(w.emitted('archive')).toHaveLength(1)
  })

  it('shows the unread dot and names the count', async () => {
    await mountMenu({ unread: 3 })
    expect(wrapper!.find('.chat-more-dot').exists()).toBe(true)
    expect(trigger().getAttribute('aria-label')).toBe('More chat actions, 3 unread notifications')
    wrapper!.unmount()
    wrapper = null
    document.body.innerHTML = ''

    await mountMenu({ unread: 0 })
    expect(wrapper!.find('.chat-more-dot').exists()).toBe(false)
    expect(trigger().getAttribute('aria-label')).toBe('More chat actions')
  })

  it.each([
    { unread: 0, mcpProblems: 0, label: 'More chat actions', dot: false, marker: false },
    { unread: 3, mcpProblems: 0, label: 'More chat actions, 3 unread notifications', dot: true, marker: false },
    { unread: 0, mcpProblems: 2, label: 'More chat actions, 2 MCP connections need attention', dot: false, marker: true },
    { unread: 3, mcpProblems: 2, label: 'More chat actions, 3 unread notifications, 2 MCP connections need attention', dot: true, marker: true },
  ])('keeps independent signals: $unread unread, $mcpProblems MCP', async ({ unread, mcpProblems, label, dot, marker }) => {
    await mountMenu({ unread, mcpProblems })
    expect(trigger().getAttribute('aria-label')).toBe(label)
    expect(wrapper!.find('.chat-more-dot').exists()).toBe(dot)
    expect(wrapper!.find('.chat-mcp-indicator').exists()).toBe(marker)
    expect(useNotificationInboxStore().count).toBe(unread)
    await openMenu()
    const details = itemNamed('Chat details')!
    expect(details.querySelector('span')?.textContent).toBe('Chat details')
    if (marker) {
      const described = details.getAttribute('aria-describedby')!
      expect(document.getElementById(described)?.textContent?.trim()).toBe(`${mcpProblems} MCP connections need attention`)
      expect(details.querySelector('.chat-mcp-count')?.getAttribute('aria-hidden')).toBe('true')
    } else expect(details.getAttribute('aria-describedby')).toBeNull()
  })

  it('marked Chat details retains its exact accessible name and returns menu focus', async () => {
    const { wrapper: w } = await mountMenu({ mcpProblems: 2 })
    await openMenu()
    const details = itemNamed('Chat details')!
    expect(details.querySelector('[aria-hidden="true"].chat-mcp-count')).not.toBeNull()
    expect(details.getAttribute('aria-label')).toBeNull()
    details.click()
    await settle()
    expect(w.emitted('details')).toHaveLength(1)
    expect(document.activeElement).toBe(trigger())
  })

  it('Back goes back in app history', async () => {
    nav.canBack.value = true
    await mountMenu({ canArchive: false })
    await openMenu()
    itemNamed('Back to Home')!.click()
    await settle()
    expect(nav.back).toHaveBeenCalledTimes(1)
  })

  it('the Notifications row shows the unread count', async () => {
    await mountMenu({ unread: 3 })
    await openMenu()
    expect(itemNamed('Notifications')!.querySelector('.chat-more-count')?.textContent?.trim()).toBe('3')
  })

  it('Notifications opens the inbox sheet', async () => {
    await mountMenu({ unread: 2 })
    await openMenu()
    itemNamed('Notifications')!.click()
    await settle()
    expect(sheet()).not.toBeNull()
    expect(document.body.querySelector('.notif-backdrop')).not.toBeNull()
    expect(useNotificationInboxStore().count).toBe(2)
  })
})
