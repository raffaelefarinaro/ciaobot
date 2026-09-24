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

    const text = wrapper.get('.project-automations').text()
    expect(text).toContain('PR watcher')
    expect(text).toContain('every 10 min')
    expect(text).toContain('Send the daily brief')
    expect(text).toContain('Send the chat brief')
    // Bound to a chat outside this project.
    expect(text).not.toContain('Other watcher')
    expect(text).not.toContain('Send another brief')
    expect(wrapper.get('#project-automations-title').text()).toContain('3')
  })
})

// The Overview / Automations tabs became plain sections on one page (the
// aligned-pages redesign): every capability stays reachable without a tab
// switch, so the tablist contract no longer applies here. TabBar keeps its own
// suite for the views that still use it.
describe('ProjectView layout', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('shows context, chats and automations together, with no tabs', async () => {
    seed()
    const wrapper = await mountView()
    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(wrapper.find('.project-context').exists()).toBe(true)
    expect(wrapper.find('#project-chats-title').text()).toBe('Chats')
    expect(wrapper.find('.project-automations').exists()).toBe(true)
  })

  it('names the project without repeating its workspace', async () => {
    seed()
    const wrapper = await mountView()
    expect(wrapper.get('.project-title').text()).toBe('Upwordo')
    expect(wrapper.find('.workspace-badge').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/\bpersonal\b/i)
  })

  it('moves the activity counts to the rail, read from the store', async () => {
    const store = seed()
    store.projectStreaming = { 'chat-read': true }
    vi.spyOn(store, 'projectNeedsInput').mockReturnValue(2)
    vi.spyOn(store, 'projectUnread').mockReturnValue(1)
    const wrapper = await mountView()
    expect(wrapper.find('.project-stats').exists()).toBe(false)
    const rows = Object.fromEntries(
      wrapper.findAll('.project-rail .rail-kv').map(row => [
        row.get('span').text(),
        row.get('strong').text(),
      ]),
    )
    expect(rows['Need you']).toBe('2')
    expect(rows['Unread']).toBe('1')
    expect(rows['Active chats']).toBe(String(store.chats.filter(c => !c.archived && c.local !== false).length))
    expect(rows['Working']).toBe('1')
  })

  it('gives each chat row a status line from the same signals as Today', async () => {
    const store = seed()
    store.projectStreaming = { 'chat-read': true }
    const wrapper = await mountView()
    const subs = wrapper.findAll('.chat-row-sub').map(node => node.text())
    expect(subs).toContain('agent is working')
    expect(subs.every(text => ['waiting for you', 'agent is working', 'new reply', 'no new activity'].includes(text))).toBe(true)
  })

  it('starts a new chat from the header through the shared picker', async () => {
    seed()
    const wrapper = await mountView()
    expect(wrapper.get('.project-new-chat').attributes('aria-haspopup')).toBe('dialog')
  })
})

describe('ProjectView progressive actions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('uses a complete keyboard menu contract and restores its trigger', async () => {
    seed()
    const wrapper = await mountView({ attach: true })
    try {
      const trigger = wrapper.get<HTMLButtonElement>('[aria-label="Project actions"]')
      trigger.element.focus()
      await trigger.trigger('click')
      await nextTick()

      const item = wrapper.get<HTMLButtonElement>('[role="menuitem"]')
      expect(document.activeElement).toBe(item.element)
      await item.trigger('keydown', { key: 'Escape' })
      await nextTick()
      expect(wrapper.find('[role="menu"]').exists()).toBe(false)
      expect(document.activeElement).toBe(trigger.element)
    } finally {
      wrapper.unmount()
    }
  })

  it('resets context editing when two projects have the same context', async () => {
    const store = seed()
    store.projects[0].context = 'Shared context'
    store.projects.push({
      project_id: 'project-2',
      name: 'Second project',
      workspace: 'personal',
      context: 'Shared context',
      is_auto: false,
    } as typeof store.projects[number])
    const wrapper = await mountView()

    await wrapper.get('.project-context').get('button').trigger('click')
    await wrapper.get('textarea').setValue('Draft for the first project')
    await wrapper.setProps({ projectId: 'project-2' })
    await nextTick()

    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.get('.context-display').text()).toContain('Shared context')
    await wrapper.get('.project-context').get('button').trigger('click')
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('Shared context')
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
    taskStore.scheduleLoading = true
    taskStore.schedulesLoaded = false
    vi.spyOn(taskStore, 'fetchSchedules').mockReturnValue(new Promise(() => {}))

    const wrapper = await mountView()

    expect(wrapper.get('.project-automations').text()).toContain('Loading automations')
    expect(wrapper.get('.project-automations').text()).not.toContain('No automations deliver')
    // A count it cannot vouch for is omitted, not printed as 0.
    expect(wrapper.get('#project-automations-title').text()).not.toContain('0')
  })

  it('reports a failed load rather than claiming there are none', async () => {
    seed()
    const taskStore = useTaskStore()
    taskStore.scheduleLoading = false
    taskStore.schedulesLoaded = false
    taskStore.scheduleLoadError = 'offline'
    vi.spyOn(taskStore, 'fetchSchedules').mockRejectedValue(new Error('offline'))

    const wrapper = await mountView()

    expect(wrapper.get('.project-automations').text()).toContain('Could not load automations')
    expect(wrapper.get('#project-automations-title').text()).not.toContain('0')
  })

  it('reports a real zero once the load resolves', async () => {
    seed()
    const taskStore = useTaskStore()
    taskStore.scheduleLoading = false
    taskStore.schedulesLoaded = true
    taskStore.scheduleLoadError = ''
    const wrapper = await mountView()

    // A resolved zero is stated in words; the heading carries no "0" count.
    expect(wrapper.get('.project-automations').text()).toContain('No automations deliver prompts to this project')
  })
})
