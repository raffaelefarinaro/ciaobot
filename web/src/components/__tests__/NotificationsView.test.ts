// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import NotificationsView from '../NotificationsView.vue'
import source from '../NotificationsView.vue?raw'
import { useNotificationInboxStore } from '../../stores/notificationInbox'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { api } from '../../lib/api'
import { readyHealth } from '../../lib/sourceHealth'
import type { SourceHealth } from '../../lib/sourceHealth'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Empty = defineComponent({ template: '<div />' })
const PaneHeader = defineComponent({ template: '<header />' })

const AT = '2026-10-26T10:00:00.000Z'
const stale: SourceHealth = { status: 'stale', lastSuccessAt: AT, error: 'x' }
const unavailable: SourceHealth = { status: 'unavailable', lastSuccessAt: '', error: 'x' }
const checking: SourceHealth = { status: 'checking', lastSuccessAt: '', error: '' }

interface Opts {
  chats?: Record<string, unknown>[]
  health?: SourceHealth
}

function chat(id: string, overrides: Record<string, unknown> = {}) {
  return {
    chat_id: id,
    project_id: 'p1',
    title: `Chat ${id}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:00:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
    ...overrides,
  }
}

async function mountView(opts: Opts = {}) {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  projects.projects = [{ project_id: 'p1', name: 'General', workspace: 'personal' }] as never
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = (opts.chats ?? []) as never
  projects.chatsHealth = opts.health ?? readyHealth()
  taskSignals.healthWorkspace = 'personal'
  taskSignals.loadedWorkspace = 'personal'
  taskSignals.health = readyHealth()

  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/chat/:chatId', component: Empty },
      { path: '/', component: Empty },
    ],
  })
  await router.push('/')
  const wrapper = mount(NotificationsView, {
    global: { plugins: [router], stubs: { PaneHeader } },
  })
  await flushPromises()
  return { wrapper, router, projects, taskSignals, inbox: useNotificationInboxStore() }
}

describe('NotificationsView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(api.post).mockReset()
    vi.mocked(api.patch).mockReset()
    vi.mocked(api.del).mockReset()
  })

  it('renders_three_sections_in_order', async () => {
    const { wrapper } = await mountView({
      chats: [
        chat('a', { pending_permission: { tool: 'Bash' } }),
        chat('b', { last_error: 'boom' }),
        chat('c', { last_activity_at: '2026-10-26T09:30:00Z' }),
      ],
    })
    const headings = wrapper.findAll('h3').map((h) => h.text())
    expect(headings).toEqual(['Needs you 2', 'New replies 1'])
    expect(wrapper.text()).not.toContain('To review')
    expect(wrapper.find('[data-testid="row-chat:a"]').exists()).toBe(true)
    expect(wrapper.findAll('.notif-section-count').map((s) => s.text())).toEqual(['2', '1'])
  })

  it('count_summary_and_all_clear_empty_state', async () => {
    const empty = await mountView()
    expect(empty.wrapper.find('[data-testid="inbox-empty-clear"]').text()).toBe("You're all caught up.")
    expect(empty.wrapper.find('[data-testid="inbox-summary"]').exists()).toBe(true)

    setActivePinia(createPinia())
    const two = await mountView({
      chats: [chat('a', { last_error: 'x' }), chat('b', { last_error: 'y' })],
    })
    expect(two.wrapper.find('[data-testid="inbox-summary"]').text()).toBe('2 items need a look.')
    expect(two.wrapper.find('[data-testid="inbox-empty-clear"]').exists()).toBe(false)
  })

  it('stale_retained_row_is_marked', async () => {
    const { wrapper } = await mountView({
      chats: [chat('a', { last_activity_at: '2026-10-26T09:30:00Z' })],
      health: stale,
    })
    expect(wrapper.find('[data-testid="row-chat:a"]').exists()).toBe(true)
    expect(wrapper.find('.notif-stale').exists()).toBe(true)
    expect(wrapper.find('[data-testid="inbox-summary"]').text()).toContain('could not be fully checked')
    expect(wrapper.find('[data-testid="source-chats"]').text()).toContain('showing the last successful check')
    expect(wrapper.find('.notif-retry').exists()).toBe(true)
    expect(wrapper.find('[data-testid="inbox-empty-clear"]').exists()).toBe(false)
  })

  it('unavailable_first_load_is_not_all_clear', async () => {
    const un = await mountView({ health: unavailable })
    expect(un.wrapper.find('[data-testid="inbox-empty-unavailable"]').exists()).toBe(true)
    expect(un.wrapper.text().toLowerCase()).not.toContain('all caught up')
    expect(un.wrapper.find('.notif-retry').exists()).toBe(true)

    setActivePinia(createPinia())
    const ch = await mountView({ health: checking })
    expect(ch.wrapper.find('[data-testid="inbox-empty-checking"]').exists()).toBe(true)
    expect(ch.wrapper.text().toLowerCase()).not.toContain('all caught up')
    expect(ch.wrapper.find('.notif-retry').exists()).toBe(false)

    setActivePinia(createPinia())
    const st = await mountView({ health: stale })
    expect(st.wrapper.find('[data-testid="inbox-empty-stale"]').exists()).toBe(true)
    expect(st.wrapper.text().toLowerCase()).not.toContain('all caught up')
  })

  it('retry_calls_source_refresh_only', async () => {
    const { wrapper, inbox, projects, taskSignals } = await mountView({ health: stale })
    const retry = vi.spyOn(inbox, 'retry')
    const sync = vi.spyOn(projects, 'syncLatest').mockResolvedValue(undefined as never)
    vi.spyOn(projects, 'fetchAll').mockResolvedValue(undefined as never)
    vi.spyOn(taskSignals, 'reload').mockResolvedValue(undefined as never)
    const markRead = vi.spyOn(projects, 'markRead')
    await wrapper.find('.notif-retry').trigger('click')
    await flushPromises()
    expect(retry).toHaveBeenCalledWith('chats')
    expect(sync).toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.del).not.toHaveBeenCalled()
    expect(markRead).not.toHaveBeenCalled()
  })

  it('text_is_not_html', async () => {
    const evil = '<img src=x onerror=alert(1)> & "q"'
    const long = 'x'.repeat(300)
    const { wrapper } = await mountView({
      chats: [chat('a', { title: evil, last_error: 'e' }), chat('b', { title: long, last_error: 'e' })],
    })
    expect(wrapper.find('img').exists()).toBe(false)
    const titles = wrapper.findAll('.notif-title').map((t) => t.text())
    expect(titles).toContain(evil)
    expect(titles).toContain(long)
    expect(wrapper.find('.notif-title').classes()).toContain('notif-title')
    expect(source).toMatch(/\.notif-title\s*{[^}]*overflow-wrap: anywhere/)
    expect(source).not.toContain('v-html')
  })

  it('no_history_dismiss_or_mark_all_controls', async () => {
    const { wrapper } = await mountView({
      chats: [chat('a', { last_error: 'e' })],
      health: stale,
    })
    const buttons = wrapper.findAll('button')
    expect(buttons.length).toBeGreaterThan(0)
    for (const b of buttons) expect(b.text()).toBe('Retry')
    expect(wrapper.text()).not.toMatch(/dismiss|snooze|mark all|clear all|history/i)
  })

  it('opening_view_never_marks_read', async () => {
    const { wrapper, router, projects } = await mountView({
      chats: [chat('a', { last_activity_at: '2026-10-26T09:30:00Z' })],
    })
    const markRead = vi.spyOn(projects, 'markRead')
    expect(wrapper.find('.notif-unread').exists()).toBe(true)
    expect(markRead).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    await wrapper.find('.notif-row').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/chat/a')
    expect(markRead).not.toHaveBeenCalled()
  })

  it('permission_stays_visible_after_read', async () => {
    const { wrapper } = await mountView({
      chats: [chat('a', { pending_permission: { tool: 'Bash' } })],
    })
    expect(wrapper.find('[data-testid="row-chat:a"]').exists()).toBe(true)
    expect(wrapper.findAll('h3').map((h) => h.text())[0]).toContain('Needs you')
    expect(wrapper.find('.notif-unread').exists()).toBe(false)
  })

  it('row_link_href_encodes_destination', async () => {
    const { wrapper } = await mountView({ chats: [chat('a/b c', { last_error: 'e' })] })
    expect(wrapper.find('.notif-row').attributes('href')).toBe('/chat/a%2Fb%20c')
  })

  it('row_controls_are_44px', () => {
    expect(source).toMatch(/\.notif-row\s*{[^}]*min-height: var\(--touch\)/)
    expect(source).toMatch(/\.notif-retry\s*{[^}]*min-height: var\(--touch\)/)
  })
})
