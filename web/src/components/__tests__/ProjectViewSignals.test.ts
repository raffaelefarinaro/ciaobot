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
  taskStore.loops = [] as unknown as typeof taskStore.loops
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  return store
}

async function mountView() {
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    props: { projectId: 'project-1' },
    global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
  })
  await nextTick()
  return wrapper
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

  it('lists only loops and schedules associated with the project', async () => {
    seed()
    const taskStore = useTaskStore()
    taskStore.loops = [{
      loop_id: 'loop-project',
      prompt: 'Check the project PRs',
      web_chat_id: 'chat-read',
      created_at: '2026-08-01T00:00:00Z',
      interval_minutes: 10,
      title: 'PR watcher',
      autostart: false,
      last_run_at: '',
      last_status: '',
      running: true,
      context_label: 'Read chat',
      next_run: null,
    }, {
      loop_id: 'loop-other',
      prompt: 'Check another project',
      web_chat_id: 'chat-missing',
      created_at: '2026-08-01T00:00:00Z',
      interval_minutes: 20,
      title: 'Other watcher',
      autostart: false,
      last_run_at: '',
      last_status: '',
      running: false,
      context_label: 'Other chat',
      next_run: null,
    }] as unknown as typeof taskStore.loops
    taskStore.schedules = [{
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

    await wrapper.get('[data-tab="loops"]').trigger('click')
    expect(wrapper.get('.automation-card').text()).toContain('PR watcher')
    expect(wrapper.get('.automation-card').text()).not.toContain('Other watcher')
    expect(wrapper.get('[data-tab="loops"]').text()).toContain('1')

    await wrapper.get('[data-tab="schedules"]').trigger('click')
    expect(wrapper.get('.automation-card').text()).toContain('Send the daily brief')
    expect(wrapper.get('.automation-card').text()).toContain('Send the chat brief')
    expect(wrapper.get('.automation-card').text()).not.toContain('Send another brief')
    expect(wrapper.get('[data-tab="schedules"]').text()).toContain('2')
  })
})
