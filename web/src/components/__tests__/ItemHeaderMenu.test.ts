// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import type { VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick } from 'vue'
import { DropdownMenuItem } from 'reka-ui'
import ItemHeaderMenu from '../ItemHeaderMenu.vue'
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

// The inbox's unread count comes from the projects' unread chats, as in ChatHeaderMoreMenu's test.
function seedUnread(count: number) {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  projects.projects = [{ project_id: 'p1', name: 'General', workspace: 'personal' }] as never
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = Array.from({ length: count }, (_, i) => ({
    chat_id: `c${i}`,
    project_id: 'p1',
    title: `Chat ${i}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:30:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
  })) as never
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

async function mountMenu(opts: { unread?: number } = {}) {
  setActivePinia(createPinia())
  seedUnread(opts.unread ?? 0)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: Empty }],
  })
  await router.push('/')
  // The host's item action, as a DropdownMenuItem, like the automation and task headers.
  const slot = () => h(DropdownMenuItem, { asChild: true }, () => h('button', { type: 'button' }, 'Run now'))
  wrapper = mount(ItemHeaderMenu, {
    props: { label: 'More automation actions' },
    slots: { default: slot },
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  return wrapper
}

const trigger = () => wrapper!.get<HTMLButtonElement>('button.item-menu-trigger').element
const items = () => Array.from(document.body.querySelectorAll<HTMLElement>('[role="menuitem"]'))

async function openMenu() {
  trigger().click()
  await flushPromises()
  await nextTick()
}

describe('ItemHeaderMenu', () => {
  beforeEach(() => {
    nav.canBack.value = false
    nav.back.mockReset()
  })
  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.innerHTML = ''
  })

  it('lists the host item and Notifications; Back only with history', async () => {
    await mountMenu()
    await openMenu()
    expect(items().map(el => el.textContent?.trim())).toEqual(['Run now', 'Notifications'])
    wrapper!.unmount()
    wrapper = null
    document.body.innerHTML = ''

    nav.canBack.value = true
    await mountMenu()
    await openMenu()
    expect(items().map(el => el.textContent?.trim())).toEqual(['Run now', 'Notifications', 'Back to Home'])
  })

  it('names the unread count on the trigger and shows the dot while unread', async () => {
    await mountMenu({ unread: 3 })
    expect(useNotificationInboxStore().count).toBe(3)
    expect(wrapper!.find('.item-menu-dot').exists()).toBe(true)
    expect(trigger().getAttribute('aria-label')).toBe('More automation actions, 3 unread notifications')
    wrapper!.unmount()
    wrapper = null
    document.body.innerHTML = ''

    await mountMenu({ unread: 0 })
    expect(wrapper!.find('.item-menu-dot').exists()).toBe(false)
    expect(trigger().getAttribute('aria-label')).toBe('More automation actions')
  })
})
