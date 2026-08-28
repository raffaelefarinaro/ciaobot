// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

function seed() {
  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'Upwordo',
    workspace: 'personal',
    is_auto: false,
  }] as unknown as typeof store.projects
  store.chats = [
    {
      chat_id: 'chat-read',
      project_id: 'project-1',
      title: 'Read chat',
      model: 'opus',
      archived: false,
      local: true,
      created_at: '2026-08-01T00:00:00Z',
      last_activity_at: '2026-08-01T00:00:00Z',
      last_read_at: '2026-08-02T00:00:00Z',
    },
    {
      chat_id: 'chat-unread',
      project_id: 'project-1',
      title: 'Unread chat',
      model: 'opus',
      archived: false,
      local: true,
      created_at: '2026-08-01T00:00:00Z',
      last_activity_at: '2026-08-03T00:00:00Z',
      last_read_at: '2026-08-01T00:00:00Z',
    },
  ] as unknown as typeof store.chats
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.bootstrapped = true
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  // ProjectView fetches these itself so it can tell "none" from "not loaded".
  // Stub them so the tests drive the store directly and stay deterministic.
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
  return store
}

async function mountView(options: { attach?: boolean } = {}) {
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    props: { projectId: 'project-1' },
    // Focus assertions need the tabs in the real document.
    ...(options.attach ? { attachTo: document.body } : {}),
    global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
  })
  // Let onMounted's reloadAll settle so the automations load state resolves.
  await flush()
  return wrapper
}

async function flush() {
  await nextTick()
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

describe('ProjectView chat rows', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  // The old markup printed `chatUnread()` as a badge digit. That getter is
  // binary, so the badge could only ever read "1" — unread is title weight now.
  it('carries chat-level unread as title weight, not a digit', async () => {
    seed()
    const wrapper = await mountView()

    const names = wrapper.findAll('.chat-name')
    const unread = names.find(n => n.text() === 'Unread chat')
    const read = names.find(n => n.text() === 'Read chat')

    expect(unread?.classes()).toContain('chat-name--unread')
    expect(read?.classes()).not.toContain('chat-name--unread')
    expect(wrapper.find('.badge').exists()).toBe(false)
  })

  it('renders state through ChatSignals rather than its own marks', async () => {
    const store = seed()
    store.projectStreaming = { 'chat-read': true }
    const wrapper = await mountView()

    expect(wrapper.find('.chat-signals').exists()).toBe(true)
    expect(wrapper.find('.chat-signal--working').exists()).toBe(true)
    // The retired local markup must not come back.
    expect(wrapper.find('.needs-input-badge').exists()).toBe(false)
    expect(wrapper.find('.spinner-dot').exists()).toBe(false)
  })

  it('hues the marks from the project workspace, not the active accent', async () => {
    const store = seed()
    store.activeWorkspace = 'personal'
    const wrapper = await mountView()
    expect(wrapper.find('.chat-signals').attributes('data-workspace-color')).toBe('emerald')
  })

  it('lists only the automations associated with the project', async () => {
    seed()
    const taskStore = useTaskStore()
    taskStore.schedules = [{
      schedule_id: 'interval-project',
      daily_time_utc: '',
      prompt: 'Check the project PRs',
      chat_id: 0,
      created_at: '2026-08-01T00:00:00Z',
      timezone_name: 'Europe/Zurich',
      last_triggered_on: '',
      days_of_week: null,
      thread_id: null,
      context_label: 'Read chat',
      frequency: 'interval',
      interval_minutes: 10,
      title: 'PR watcher',
      day_of_month: null,
      run_at_date: null,
      web_chat_id: 'chat-read',
      web_project_id: null,
      workspace: 'personal',
      model: '',
      next_run: null,
      last_expected_run: null,
      missed: false,
      enabled: true,
      archive_policy: 'manual',
    }, {
      schedule_id: 'interval-other',
      daily_time_utc: '',
      prompt: 'Check another project',
      chat_id: 0,
      created_at: '2026-08-01T00:00:00Z',
      timezone_name: 'Europe/Zurich',
      last_triggered_on: '',
      days_of_week: null,
      thread_id: null,
      context_label: 'Other chat',
      frequency: 'interval',
      interval_minutes: 20,
      title: 'Other watcher',
      day_of_month: null,
      run_at_date: null,
      web_chat_id: 'chat-missing',
      web_project_id: null,
      workspace: 'personal',
      model: '',
      next_run: null,
      last_expected_run: null,
      missed: false,
      enabled: false,
      archive_policy: 'manual',
    }, {
      schedule_id: 'schedule-project',
      daily_time_utc: '09:00',
      prompt: 'Send the daily brief',
      chat_id: 0,
      created_at: '2026-08-01T00:00:00Z',
      timezone_name: 'Europe/Zurich',
      last_triggered_on: '',
      days_of_week: null,
      thread_id: null,
      context_label: '',
      frequency: 'daily',
      day_of_month: null,
      run_at_date: null,
      web_chat_id: null,
      web_project_id: 'project-1',
      workspace: 'personal',
      model: '',
      next_run: null,
      last_expected_run: null,
      missed: false,
      enabled: true,
      archive_policy: 'manual',
    }, {
      schedule_id: 'schedule-chat',
      daily_time_utc: '10:00',
      prompt: 'Send the chat brief',
      chat_id: 0,
      created_at: '2026-08-01T00:00:00Z',
      timezone_name: 'Europe/Zurich',
      last_triggered_on: '',
      days_of_week: null,
      thread_id: null,
      context_label: '',
      frequency: 'weekly',
      day_of_month: null,
      run_at_date: null,
      web_chat_id: 'chat-read',
      web_project_id: null,
      workspace: 'personal',
      model: '',
      next_run: null,
      last_expected_run: null,
      missed: false,
      enabled: false,
      archive_policy: 'manual',
    }, {
      schedule_id: 'schedule-other',
      daily_time_utc: '11:00',
      prompt: 'Send another brief',
      chat_id: 0,
      created_at: '2026-08-01T00:00:00Z',
      timezone_name: 'Europe/Zurich',
      last_triggered_on: '',
      days_of_week: null,
      thread_id: null,
      context_label: '',
      frequency: 'daily',
      day_of_month: null,
      run_at_date: null,
      web_chat_id: 'chat-missing',
      web_project_id: null,
      workspace: 'personal',
      model: '',
      next_run: null,
      last_expected_run: null,
      missed: false,
      enabled: true,
      archive_policy: 'manual',
    }] as unknown as typeof taskStore.schedules

    const wrapper = await mountView()

    await wrapper.get('[data-tab="schedules"]').trigger('click')
    const text = wrapper.get('.automation-card').text()
    expect(text).toContain('PR watcher')
    expect(text).toContain('every 10 min')
    expect(text).toContain('Send the daily brief')
    expect(text).toContain('Send the chat brief')
    // Bound to a chat outside this project.
    expect(text).not.toContain('Other watcher')
    expect(text).not.toContain('Send another brief')
    expect(wrapper.get('[data-tab="schedules"]').text()).toContain('3')
  })
})

// The tab bar shipped with role="tab" on buttons that had no enclosing
// tablist, no aria-controls, no tabpanel to point at, and no keyboard
// handling at all. These lock the full pattern in.
describe('ProjectView tabs', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('owns the tabs with a tablist', async () => {
    seed()
    const wrapper = await mountView()

    const tablist = wrapper.get('[role="tablist"]')
    expect(tablist.attributes('aria-label')).toBe('Project sections')
    const tabs = tablist.findAll('[role="tab"]')
    expect(tabs).toHaveLength(2)
    expect(tabs.map(t => t.attributes('data-tab'))).toEqual(['overview', 'schedules'])
  })

  it('tracks the active tab with aria-selected', async () => {
    seed()
    const wrapper = await mountView()

    expect(wrapper.get('[data-tab="overview"]').attributes('aria-selected')).toBe('true')
    expect(wrapper.get('[data-tab="schedules"]').attributes('aria-selected')).toBe('false')

    await wrapper.get('[data-tab="schedules"]').trigger('click')

    expect(wrapper.get('[data-tab="overview"]').attributes('aria-selected')).toBe('false')
    expect(wrapper.get('[data-tab="schedules"]').attributes('aria-selected')).toBe('true')
  })

  // aria-controls must name a real tabpanel, and that panel must point back.
  it.each(['overview', 'schedules'])('pairs the %s tab with its panel', async (key) => {
    seed()
    const wrapper = await mountView()
    await wrapper.get(`[data-tab="${key}"]`).trigger('click')

    const tab = wrapper.get(`[data-tab="${key}"]`)
    const tabId = tab.attributes('id')
    const controls = tab.attributes('aria-controls')
    expect(tabId).toBeTruthy()
    expect(controls).toBeTruthy()

    const panel = wrapper.get(`#${controls}`)
    expect(panel.attributes('role')).toBe('tabpanel')
    expect(panel.attributes('aria-labelledby')).toBe(tabId)
    // Panels are in the tab sequence so their content is keyboard reachable.
    expect(panel.attributes('tabindex')).toBe('0')
  })

  it('renders only the selected panel', async () => {
    seed()
    const wrapper = await mountView()
    expect(wrapper.findAll('[role="tabpanel"]')).toHaveLength(1)

    await wrapper.get('[data-tab="schedules"]').trigger('click')
    expect(wrapper.findAll('[role="tabpanel"]')).toHaveLength(1)
    expect(wrapper.get('[role="tabpanel"]').attributes('aria-labelledby'))
      .toBe(wrapper.get('[data-tab="schedules"]').attributes('id'))
  })

  // Roving tabindex: the whole bar is one Tab stop, not three.
  it('keeps a single tab stop', async () => {
    seed()
    const wrapper = await mountView()

    const tabindexes = () => wrapper.findAll('[role="tab"]').map(t => t.attributes('tabindex'))
    expect(tabindexes()).toEqual(['0', '-1'])

    await wrapper.get('[data-tab="schedules"]').trigger('click')
    expect(tabindexes()).toEqual(['-1', '0'])
  })

  it('moves right and left with the arrow keys, wrapping at the ends', async () => {
    seed()
    const wrapper = await mountView()

    await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.get('[data-tab="schedules"]').attributes('aria-selected')).toBe('true')

    // Wraps forward past the last tab...
    await wrapper.get('[data-tab="schedules"]').trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.get('[data-tab="overview"]').attributes('aria-selected')).toBe('true')

    // ...and backward past the first.
    await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.get('[data-tab="schedules"]').attributes('aria-selected')).toBe('true')
  })

  it('jumps to the first and last tab with Home and End', async () => {
    seed()
    const wrapper = await mountView()

    await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'End' })
    expect(wrapper.get('[data-tab="schedules"]').attributes('aria-selected')).toBe('true')

    await wrapper.get('[data-tab="schedules"]').trigger('keydown', { key: 'Home' })
    expect(wrapper.get('[data-tab="overview"]').attributes('aria-selected')).toBe('true')
  })

  it('leaves other keys to the browser', async () => {
    seed()
    const wrapper = await mountView()

    await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'ArrowDown' })
    await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'a' })
    expect(wrapper.get('[data-tab="overview"]').attributes('aria-selected')).toBe('true')
  })

  it('moves DOM focus along with the arrow keys', async () => {
    seed()
    const wrapper = await mountView({ attach: true })
    try {
      const overview = wrapper.get('[data-tab="overview"]').element as HTMLElement
      overview.focus()
      expect(document.activeElement).toBe(overview)

      await wrapper.get('[data-tab="overview"]').trigger('keydown', { key: 'ArrowRight' })
      expect(document.activeElement).toBe(wrapper.get('[data-tab="schedules"]').element)
    } finally {
      wrapper.unmount()
    }
  })
})

// Rule S6: an empty list must not claim absence when the fetch never resolved.
describe('ProjectView automations load state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('says it is loading rather than claiming there are none', async () => {
    seed()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockReturnValue(new Promise(() => {}))

    const wrapper = await mountView()
    await wrapper.get('[data-tab="schedules"]').trigger('click')

    expect(wrapper.get('.automation-card').text()).toContain('loading automations')
    expect(wrapper.get('.automation-card').text()).not.toContain('no automations deliver')
    // A count it cannot vouch for is omitted, not printed as 0.
    expect(wrapper.get('[data-tab="schedules"]').text()).not.toContain('0')
  })

  it('reports a failed load rather than claiming there are none', async () => {
    seed()
    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockRejectedValue(new Error('offline'))

    const wrapper = await mountView()
    await wrapper.get('[data-tab="schedules"]').trigger('click')

    expect(wrapper.get('.automation-card').text()).toContain('could not load automations')
    expect(wrapper.get('[data-tab="schedules"]').text()).not.toContain('0')
  })

  it('reports a real zero once the load resolves', async () => {
    seed()
    const wrapper = await mountView()
    await wrapper.get('[data-tab="schedules"]').trigger('click')

    expect(wrapper.get('.automation-card').text()).toContain('no automations deliver prompts to this project')
    expect(wrapper.get('[data-tab="schedules"]').text()).toContain('0')
  })
})
