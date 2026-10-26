// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import HomeGlance from '../HomeGlance.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useTaskStore } from '../../stores/tasks'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useInboxOpenRequests } from '../../composables/useInboxPopover'
import { api } from '../../lib/api'
import { readyHealth } from '../../lib/sourceHealth'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Empty = defineComponent({ template: '<div />' })

function chat(id: string, overrides: Record<string, unknown> = {}) {
  return {
    chat_id: id,
    project_id: 'p1',
    title: `Chat ${id}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:30:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
    ...overrides,
  }
}

async function mountGlance(chats: Record<string, unknown>[] = []) {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  projects.projects = [{ project_id: 'p1', name: 'General', workspace: 'personal' }] as never
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = chats as never
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
  vi.mocked(api.get).mockResolvedValue([])

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/chat/:chatId', component: Empty }, { path: '/', component: Empty }],
  })
  await router.push('/')
  const wrapper = mount(HomeGlance, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

const NEEDS = { pending_permission: { tool: 'Bash' } }

describe('HomeGlance', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('title_is_at_a_glance', async () => {
    const wrapper = await mountGlance()
    const section = wrapper.get('section.home-glance')
    expect(section.attributes('aria-labelledby')).toBe('home-glance-title')
    expect(wrapper.get('h2#home-glance-title').text()).toBe('At a glance')
  })

  it('heading_status_counts_items', async () => {
    const wrapper = await mountGlance([chat('a', NEEDS), chat('b')])
    expect(wrapper.get('.home-glance-status').text()).toBe('2 need a look')
  })

  it('all_clear_says_nothing_to_review', async () => {
    const wrapper = await mountGlance()
    expect(wrapper.get('.home-glance-status').text()).toBe('Nothing to review.')
    expect(wrapper.get('.home-glance-summary').text()).toBe('Nothing to review.')
  })

  it('narrow_summary_lists_nonempty_sections', async () => {
    const wrapper = await mountGlance([chat('a', NEEDS), chat('b')])
    expect(wrapper.get('.home-glance-summary').text()).toBe('Needs you 1 · New replies 1')
  })

  it('narrow_summary_button_requests_inbox_open', async () => {
    const wrapper = await mountGlance([chat('a', NEEDS)])
    const requests = useInboxOpenRequests()
    const before = requests.value
    const button = wrapper.get('button.home-glance-summary')
    expect(button.attributes('aria-haspopup')).toBe('dialog')
    await button.trigger('click')
    expect(requests.value).toBe(before + 1)
  })

  it('wide_list_renders_inbox_sections', async () => {
    const wrapper = await mountGlance([chat('a', NEEDS), chat('b')])
    const list = wrapper.get('.home-glance-list')
    expect(list.findAll('section.notif-section')).toHaveLength(2)
    expect(list.find('[data-testid="row-chat:a"]').exists()).toBe(true)
    expect(list.find('[data-testid="inbox-summary"]').exists()).toBe(false)
  })
})
