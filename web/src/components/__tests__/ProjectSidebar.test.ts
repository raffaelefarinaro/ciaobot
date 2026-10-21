// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { Task } from '../../lib/types'
import { pendingPrompt } from '../../lib/prompt'

const chatId = 'chat-1234-abcd'

function delegatedTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 'task-1', title: 'Ship it', status: 'in_review', project_id: '', due: '',
    assignee: 'agent', chat_id: '', attempt_id: 'attempt-1', created_at: '', updated_at: '',
    revision: 'r1', relative_path: 'tasks/ship-it.md', attempt_state: 'ready_for_review',
    attempt_outcome: 'done', attempt_summary: '', attempt_detail: '', live_attempt_id: 'attempt-1',
    ...overrides,
  } as Task
}

async function mountSidebar() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
  await router.isReady()
  return mount(ProjectSidebar, {
    attachTo: document.body,
    props: { collapsed: false, mode: 'chat' },
    global: { plugins: [router] },
  })
}

describe('ProjectSidebar chat actions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.unread = {}
    store.workspaces = [{
      name: 'personal',
      vault_root: '/tmp/vault',
      default_provider: 'claude',
      gws_profile: '',
    }]
    store.projects = [{
      project_id: 'project-1',
      name: 'General',
      workspace: 'personal',
      context: '',
      created_at: '2026-07-29T00:00:00Z',
      order: 0,
      vault_folder: 'general',
      is_auto: true,
    }]
    store.chats = [{
      chat_id: chatId,
      project_id: 'project-1',
      title: 'Copy me',
      model: 'sonnet',
      provider: 'claude',
      mode: 'default',
      session_id: 'session-1',
      created_at: '2026-07-29T00:00:00Z',
      archived: false,
    }]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('shows a notification dot beside an unread chat', async () => {
    const store = useProjectStore()
    store.chats[0].last_activity_at = '2026-08-12T10:00:00Z'
    store.chats[0].last_read_at = '2026-08-12T09:00:00Z'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    const unread = wrapper.get('.chat-item .chat-signal--unread')
    expect(unread.attributes('aria-label')).toBe('Unread chat')
    expect(unread.attributes('title')).toBe('Unread chat')

    wrapper.unmount()
  })

  it('shows the workspace attention count on the Home item', async () => {
    const store = useProjectStore()
    store.chats[0].last_activity_at = '2026-08-12T10:00:00Z'
    store.chats[0].last_read_at = '2026-08-12T09:00:00Z'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })

    const chatsLink = wrapper.get('a[href="/"]')
    // A subtle number from a data attribute, not a dot or a pill.
    expect(chatsLink.attributes('data-count')).toBe('1')
    expect(chatsLink.attributes('aria-label')).toBe('Home — 1 chat needs attention')

    wrapper.unmount()
  })

  it('counts tasks waiting for review on the Tasks item, quietly, and hides zero', async () => {
    const signals = useTaskSignalsStore()
    signals.tasks = [
      delegatedTask(),
      delegatedTask({ id: 'task-2', live_attempt_id: 'attempt-2', attempt_id: 'attempt-2' }),
      delegatedTask({ id: 'task-3', status: 'in_progress', attempt_state: 'running', attempt_outcome: '' }),
    ]
    signals.loadedWorkspace = 'personal'
    const wrapper = await mountSidebar()

    const tasksLink = wrapper.get('a[href="/tasks"]')
    expect(tasksLink.attributes('data-count')).toBe('2')
    expect(tasksLink.attributes('aria-label')).toBe('tasks — 2 waiting for review')

    signals.tasks = []
    await nextTick()
    expect(tasksLink.attributes('data-count')).toBeUndefined()
    expect(tasksLink.attributes('aria-label')).toBe('tasks')
    wrapper.unmount()
  })

  it('tags a delegated task chat in words and keeps the full title in its tooltip', async () => {
    const store = useProjectStore()
    store.chats[0].helper = {
      kind: 'task_delegation', task_id: 'task-1', task_revision: 'r1', attempt_id: 'attempt-1',
    }
    store.chats.push({ ...store.chats[0], chat_id: 'plain', title: 'Plain chat', helper: undefined })
    const wrapper = await mountSidebar()

    const rows = wrapper.findAll('.chat-item')
    const delegated = rows.find(row => row.text().includes('Copy me'))!
    const plain = rows.find(row => row.text().includes('Plain chat'))!
    expect(delegated.get('.chat-task-tag').text()).toBe('task')
    // The row's accessible name comes from its content, so it says "task" too.
    expect(delegated.text()).toContain('task')
    expect(plain.find('.chat-task-tag').exists()).toBe(false)
    expect(delegated.attributes('title')).toBe('Copy me\nDrag to move to another project')
    wrapper.unmount()
  })

  it('tags a chat a loaded task points at even before its stamp arrives', async () => {
    const signals = useTaskSignalsStore()
    signals.tasks = [delegatedTask({ chat_id: chatId })]
    signals.loadedWorkspace = 'personal'
    const wrapper = await mountSidebar()
    expect(wrapper.get('.chat-item .chat-task-tag').text()).toBe('task')
    wrapper.unmount()
  })

  it('summarises a project only while it is collapsed, with a dot and no count', async () => {
    const store = useProjectStore()
    store.chats[0].last_activity_at = '2026-08-12T10:00:00Z'
    store.chats[0].last_read_at = '2026-08-12T09:00:00Z'
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    await nextTick()

    const projectId = store.chats[0].project_id
    const group = () => wrapper.findAll('.project-group').find(g => g.find('.project-name').text().includes(
      store.projects.find(p => p.project_id === projectId)!.name,
    ))!
    const chevron = () => group().get('.project-icon')
    // Make sure the project is expanded, then collapsed, whatever it starts as.
    if (chevron().attributes('aria-expanded') !== 'true') await chevron().trigger('click')
    expect(group().find('.project-dot').exists()).toBe(false)
    await chevron().trigger('click')
    expect(chevron().attributes('aria-expanded')).toBe('false')
    const dot = group().get('.project-dot')
    expect(dot.text()).toBe('')
    expect(dot.attributes('aria-label')).toBe('1 unread')
    wrapper.unmount()
  })

  it('copies the selected chat ID from the action menu', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    await wrapper.get('[aria-label="Chat actions"]').trigger('click')
    const copyButton = Array.from(document.body.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Copy chat ID')
    expect(copyButton).toBeTruthy()
    copyButton!.click()
    await flushPromises()

    expect(writeText).toHaveBeenCalledOnce()
    expect(writeText).toHaveBeenCalledWith(chatId)
    expect(useProjectStore().toasts.at(-1)).toMatchObject({
      chat_id: chatId,
      title: 'Chat ID copied',
      body: chatId,
    })

    wrapper.unmount()
  })

  it('moves a chat when it is dropped onto another project', async () => {
    const store = useProjectStore()
    store.projects.push({
      project_id: 'project-2',
      name: 'Second project',
      workspace: 'personal',
      context: '',
      created_at: '2026-07-29T00:00:00Z',
      order: 1,
      vault_folder: '',
      is_auto: false,
    })
    const moveChat = vi.spyOn(store, 'moveChat').mockResolvedValue({
      ...store.chats[0],
      project_id: 'project-2',
    })
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    })

    const chat = wrapper.get('.chat-item')
    const dataTransfer = {
      effectAllowed: '',
      setData: vi.fn(),
    }
    const dragStart = new Event('dragstart', { bubbles: true })
    Object.defineProperty(dragStart, 'dataTransfer', { value: dataTransfer })
    chat.element.dispatchEvent(dragStart)

    const target = wrapper.findAll('.project-header')[1]
    await target.trigger('dragover')
    expect(target.classes()).toContain('drag-over')
    await target.trigger('drop')
    await flushPromises()

    expect(dataTransfer.setData).toHaveBeenCalledWith('application/x-ciaobot-chat', chatId)
    expect(moveChat).toHaveBeenCalledWith(chatId, 'project-2')

    wrapper.unmount()
  })

  // Running subagents are listed in the Chat details rail, not as
  // rows under the chat in this tree.
  it('does not list running subagents as rows in the tree', async () => {
    const store = useProjectStore()
    store.runningSubagents = {
      [chatId]: [{ agent_id: 'a1b2c3d4', description: 'Sweep the callers', status: 'running' }],
    }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/chat/:chatId/subagent/:agentId', component: { template: '<div />' } },
      ],
    })
    await router.push(`/chat/${chatId}/subagent/a1b2c3d4`)
    await router.isReady()

    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    expect(wrapper.findAll('.chat-item')).toHaveLength(1)
    expect(wrapper.text()).not.toContain('Sweep the callers')
    expect(wrapper.find('.subagent-toggle').exists()).toBe(false)

    wrapper.unmount()
  })

  async function mountWithProjectRoute() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/project/:projectId', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    return { router, wrapper }
  }

  it('opens the new project after creating it from the sidebar dialog', async () => {
    const store = useProjectStore()
    store.bootstrapped = true
    const create = vi.spyOn(store, 'createProject').mockResolvedValue({
      ...store.projects[0],
      project_id: 'project-9',
      name: 'Research',
      is_auto: false,
    })
    const { router, wrapper } = await mountWithProjectRoute()

    await wrapper.get('button[aria-label="New project"]').trigger('click')
    await flushPromises()
    expect(pendingPrompt.value?.title).toBe('New project')
    pendingPrompt.value!.resolve('Research')
    await flushPromises()

    expect(create).toHaveBeenCalledWith('Research')
    expect(router.currentRoute.value.path).toBe('/project/project-9')
    expect(wrapper.emitted('chat-selected')).toBeTruthy()

    wrapper.unmount()
  })

  it('stays put and shows a toast when creating the project fails', async () => {
    const store = useProjectStore()
    store.bootstrapped = true
    vi.spyOn(store, 'createProject').mockRejectedValue(new Error('boom'))
    const toast = vi.spyOn(store, 'pushErrorToast')
    const { router, wrapper } = await mountWithProjectRoute()

    await wrapper.get('button[aria-label="New project"]').trigger('click')
    await flushPromises()
    pendingPrompt.value!.resolve('Research')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/')
    expect(toast).toHaveBeenCalledWith('Could not create project', expect.anything())

    wrapper.unmount()
  })

  it('does nothing when the dialog is cancelled', async () => {
    const store = useProjectStore()
    store.bootstrapped = true
    const create = vi.spyOn(store, 'createProject')
    const { router, wrapper } = await mountWithProjectRoute()

    await wrapper.get('button[aria-label="New project"]').trigger('click')
    await flushPromises()
    pendingPrompt.value!.resolve(null)
    await flushPromises()

    expect(create).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/')

    wrapper.unmount()
  })

  it('shows the naming placeholder while a chat title is pending, then the title', async () => {
    const store = useProjectStore()
    store.chats[0].title = 'New Chat'
    store.chats[0].title_status = 'pending'
    const wrapper = await mountSidebar()
    await flushPromises()

    const shimmer = wrapper.get('.chat-item .title-shimmer')
    expect(shimmer.attributes('aria-label')).toBe('Naming conversation')
    expect(shimmer.attributes('title')).toBe('Naming conversation')
    // The placeholder stands in for the title, so the temporary one never shows.
    expect(wrapper.get('.chat-item').text()).not.toContain('New Chat')

    store.chats[0].title = 'Deploy the API'
    store.chats[0].title_status = 'ready'
    await nextTick()

    expect(wrapper.find('.chat-item .title-shimmer').exists()).toBe(false)
    expect(wrapper.get('.chat-item .chat-title').text()).toBe('Deploy the API')

    wrapper.unmount()
  })

  it('pulls in the shared naming placeholder stylesheet', () => {
    // The sidebar's placeholder and the chat header's are the same bar: the
    // sidebar loads the shared file, so the two cannot drift apart.
    const here = join(__dirname, '..')
    const source = readFileSync(join(here, 'ProjectSidebar.vue'), 'utf8')
    expect(source).toContain('<style scoped src="./titleShimmer.css">')
    const css = readFileSync(join(here, 'titleShimmer.css'), 'utf8')
    expect(css).toMatch(/\.title-shimmer\s*\{[^}]*flex:\s*1/)
    expect(css).toMatch(/animation:\s*title-shimmer-sweep/)
    // The extracted stylesheet cannot declare keyframes for another block, so
    // the mobile skeleton keeps its own sweep in the sidebar's block.
    expect(source).toMatch(/animation:\s*mm-shimmer-sweep/)
    expect(source).toMatch(/@keyframes\s+mm-shimmer-sweep/)
  })
})

describe('ProjectSidebar global new chat', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [{
      name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '',
    }]
    store.activeWorkspace = 'personal'
    store.projects = [{
      project_id: 'project-1', name: 'General', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 0, vault_folder: 'general', is_auto: true,
    }]
    store.chats = []
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  async function mountSidebar(mode: 'chat' | 'memory' | 'schedules') {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    return mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode },
      global: { plugins: [router] },
    })
  }

  it('offers one New chat above the tree, opening the shared picker', async () => {
    const { pendingNewChat } = await import('../../lib/newChat')
    const wrapper = await mountSidebar('chat')
    const button = wrapper.get('.sidebar-new-chat')

    expect(button.attributes('aria-haspopup')).toBe('dialog')
    expect(button.attributes('aria-label')).toBe('New chat in Personal')
    expect(button.text()).toContain('New chat')

    await button.trigger('click')
    // The shared picker is the one project-selection path; the sidebar just
    // opens it in the active workspace rather than growing its own selector.
    expect(pendingNewChat.value?.options).toEqual({ workspace: 'personal', projectId: undefined })
    pendingNewChat.value?.resolve(null)
    await nextTick()
    wrapper.unmount()
  })

  it('hides the global New chat in modes without a project tree', async () => {
    const wrapper = await mountSidebar('memory')
    expect(wrapper.find('.sidebar-new-chat').exists()).toBe(false)
    wrapper.unmount()
  })

  it('puts New project and the archive on the Projects label, not in a footer', async () => {
    useProjectStore().bootstrapped = true
    const wrapper = await mountSidebar('chat')
    const row = wrapper.get('.sidebar-label-row')
    expect(row.text()).toContain('Projects')
    expect(row.get('button[aria-label="New project"]').text()).toBe('New')
    expect(row.find('button[aria-label="Completed projects"]').exists()).toBe(true)
    expect(wrapper.find('.sidebar-footer').exists()).toBe(false)
    wrapper.unmount()
  })

  it('puts New automation on the Routines label', async () => {
    const wrapper = await mountSidebar('schedules')
    useTaskStore().loading = false
    await nextTick()
    const button = wrapper.get('button[aria-label="New automation"]')
    expect(button.text()).toBe('New')
    await button.trigger('click')
    expect(wrapper.emitted('new-schedule')).toHaveLength(1)
    expect(wrapper.find('.sidebar-footer').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows routine times in the browser zone, not the schedule zone', async () => {
    const previousTz = process.env.TZ
    process.env.TZ = 'Europe/Rome'
    try {
      useTaskStore().schedules = [{
        schedule_id: 'utc-routine',
        title: 'Early sweep',
        prompt: 'sweep',
        daily_time_utc: '00:01',
        timezone_name: 'UTC',
        frequency: 'daily',
        enabled: true,
        scope: 'user',
        workspace: 'personal',
        web_project_id: 'proj-1',
        next_run: '2026-12-05T00:01:00Z',
        last_triggered_on: '',
        days_of_week: null,
        interval_minutes: 0,
        day_of_month: null,
        run_at_date: null,
        web_chat_id: null,
        missed: false,
        model: '',
        archive_policy: 'manual',
      } as never]
      const wrapper = await mountSidebar('schedules')
      useTaskStore().loading = false
      await nextTick()
      const row = wrapper.get('.schedule-item .schedule-time')
      expect(row.text()).toBe('01:01')
      wrapper.unmount()
    } finally {
      if (previousTz === undefined) delete process.env.TZ
      else process.env.TZ = previousTz
    }
  })

  it('leaves the vault lists to the map rail', async () => {
    const wrapper = await mountSidebar('memory')
    expect(wrapper.text()).not.toContain('Most connected')
    expect(wrapper.text()).not.toContain('Recently written')
    wrapper.unmount()
  })
})

describe('ProjectSidebar update badge', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  function mountSidebar() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    return router.push('/').then(() => router.isReady()).then(() => mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: {
        plugins: [router],
      },
    }))
  }

  it('marks the settings nav item with a short note when an update is available', async () => {
    const store = useProjectStore()
    store.packageStatus = {
      current_version: '0.9.1',
      latest_version: '9.9.9',
      update_available: true,
      mode: 'bundled_app',
    }
    const wrapper = await mountSidebar()
    await nextTick()

    const settingsLink = wrapper.get('a[href="/settings"]')
    expect(settingsLink.attributes('data-note')).toBe('update')

    wrapper.unmount()
  })

  it('shows no badge when already up to date', async () => {
    const store = useProjectStore()
    store.packageStatus = {
      current_version: '0.9.1',
      latest_version: '0.9.1',
      update_available: false,
      mode: 'bundled_app',
    }
    const wrapper = await mountSidebar()
    await nextTick()

    const settingsLink = wrapper.get('a[href="/settings"]')
    expect(settingsLink.find('.nav-item-badge').exists()).toBe(false)

    wrapper.unmount()
  })

  it('marks the settings nav item for a blocking housekeeping action', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [{
      id: 'gws-login',
      kind: 'gws_health',
      severity: 1,
      title: 'Sign in to gws',
      detail: 'The token needs attention.',
      glyph: '!',
      workspace: '',
      run_label: 'Open settings',
      chat_label: '',
      chat_prompt: '',
      view_label: 'Settings',
      view_route: '/settings',
      blocking: true,
    }]
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})

    const wrapper = await mountSidebar()
    const settingsLink = wrapper.get('a[href="/settings"]')

    expect(settingsLink.classes()).toContain('nav-item--warning')
    expect(settingsLink.attributes('data-note')).toBe('check')
    expect(settingsLink.attributes('aria-label')).toBe('settings — action required')

    wrapper.unmount()
  })

  function scheduleIn(workspace: string, patch: Record<string, unknown>) {
    return {
      schedule_id: `s-${workspace}-${Object.keys(patch).join('-')}`,
      title: 'Routine',
      prompt: 'go',
      frequency: 'daily',
      enabled: true,
      scope: 'user',
      workspace,
      missed: false,
      last_status: 'ok',
      ...patch,
    } as never
  }

  function seedWorkspaces() {
    const store = useProjectStore()
    store.workspaces = ['personal', 'work'].map(name => ({
      name, vault_root: `/tmp/${name}`, default_provider: 'claude', gws_profile: '',
    }))
    store.activeWorkspace = 'personal'
  }

  it('does not warn on Automations for a failure in another workspace', async () => {
    seedWorkspaces()
    useTaskStore().schedules = [
      scheduleIn('work', { last_status: 'error' }),
      scheduleIn('work', { missed: true }),
    ]
    const wrapper = await mountSidebar()
    await nextTick()
    expect(wrapper.get('a[href="/schedules"]').classes()).not.toContain('nav-item--warning')
    wrapper.unmount()
  })

  it('warns on Automations for a failure in the active workspace', async () => {
    seedWorkspaces()
    useTaskStore().schedules = [scheduleIn('personal', { last_status: 'error' })]
    const wrapper = await mountSidebar()
    await nextTick()
    expect(wrapper.get('a[href="/schedules"]').classes()).toContain('nav-item--warning')
    wrapper.unmount()
  })

  it('warns for a missing-chat schedule in the active workspace even when disabled', async () => {
    seedWorkspaces()
    useTaskStore().schedules = [
      scheduleIn('personal', { enabled: false, last_status: 'missing-chat' }),
    ]
    const wrapper = await mountSidebar()
    await nextTick()
    expect(wrapper.get('a[href="/schedules"]').classes()).toContain('nav-item--warning')
    wrapper.unmount()
  })
})

describe('ProjectSidebar accessible context menus', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [{
      name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '',
    }]
    store.projects = [{
      project_id: 'project-1', name: 'Notes', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 0, vault_folder: '', is_auto: false,
    }, {
      project_id: 'project-2', name: 'Archive', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 1, vault_folder: '', is_auto: false,
    }]
    store.chats = [{
      chat_id: 'chat-menu', project_id: 'project-1', title: 'Menu chat', model: 'sonnet',
      provider: 'claude', mode: 'default', session_id: 'session-menu',
      created_at: '2026-07-29T00:00:00Z', archived: false,
    }]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  async function mountSidebar() {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    return mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
  }

  it('opens a right-click chat menu at the pointer and dismisses outside', async () => {
    const wrapper = await mountSidebar()
    const row = wrapper.get('.chat-item')
    const event = new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 140, clientY: 90, button: 2,
    })
    row.element.dispatchEvent(event)
    await flushPromises()

    const menu = document.body.querySelector<HTMLElement>('[data-reka-menu-content]')
    expect(menu).not.toBeNull()
    expect(['right', 'left']).toContain(menu?.getAttribute('data-side'))
    expect(menu?.textContent).toContain('Copy chat ID')
    expect(event.defaultPrevented).toBe(true)

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    expect(document.body.querySelector('[data-reka-menu-content]')).toBeNull()
    wrapper.unmount()
  })

  it('exposes the same menu through the 44px keyboard/touch action trigger', async () => {
    const wrapper = await mountSidebar()
    const trigger = wrapper.get<HTMLButtonElement>('[aria-label="Chat actions"]')
    trigger.element.focus()
    await trigger.trigger('click')
    await flushPromises()

    expect(document.body.querySelector('[data-reka-menu-content]')?.getAttribute('role')).toBe('menu')
    expect(document.body.textContent).toContain('Move to...')
    // The explicit trigger remains a native button with a touch-sized class;
    // unlike a hover-only affordance it is reachable on a phone.
    expect(trigger.element.tagName).toBe('BUTTON')
    expect(trigger.classes()).toContain('chat-actions-btn')
    wrapper.unmount()
  })

  it('keeps project actions keyboard-reachable while retaining pointer context placement', async () => {
    const wrapper = await mountSidebar()
    const projectHeader = wrapper.findAll('.project-header')[0]
    const event = new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, clientX: 220, clientY: 120, button: 2,
    })
    projectHeader.element.dispatchEvent(event)
    await flushPromises()
    expect(document.body.textContent).toContain('Rename')
    expect(document.body.textContent).toContain('Delete')

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
    const action = wrapper.get<HTMLButtonElement>('[aria-label="Project actions"]')
    await action.trigger('click')
    await flushPromises()
    expect(document.body.querySelector('[data-reka-menu-content]')).not.toBeNull()
    expect(action.classes()).toContain('project-actions-btn')
    wrapper.unmount()
  })

  it('offers Delete on a vault-backed project and names the folder in the confirmation', async () => {
    // A vault-backed project used to have no Delete in its menu at all, so the
    // only way out was Complete. The confirmation names the folder that goes
    // from disk, so the user knows what is lost.
    const store = useProjectStore()
    store.projects = [{
      project_id: 'project-vault', name: 'Kitchen', workspace: 'personal', context: '',
      created_at: '2026-07-29T00:00:00Z', order: 0, vault_folder: 'kitchen', is_auto: false,
    }]
    store.chats = [{
      chat_id: 'chat-vault', project_id: 'project-vault', title: 'Kitchen chat', model: 'sonnet',
      provider: 'claude', mode: 'default', session_id: 'session-vault',
      created_at: '2026-07-29T00:00:00Z', archived: false,
    }]
    const { pendingConfirm } = await import('../../lib/confirm')
    const wrapper = await mountSidebar()

    await wrapper.get('[aria-label="Project actions"]').trigger('click')
    await flushPromises()
    const menu = document.body.querySelector<HTMLElement>('[data-reka-menu-content]')
    expect(menu?.textContent).toContain('Rename')
    expect(menu?.textContent).toContain('Delete')

    const deleteButton = Array.from(document.body.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.trim() === 'Delete')!
    deleteButton.click()
    await flushPromises()

    const request = pendingConfirm.value
    expect(request).toBeTruthy()
    expect(request!.title).toBe('Delete project')
    expect(request!.confirmLabel).toBe('Delete project')
    expect(request!.destructive).toBe(true)
    expect(request!.message).toContain('projects/active/kitchen/')
    expect(request!.message).toContain('1')

    wrapper.unmount()
  })

  it('keeps the Move submenu mounted for keyboard opening and returns to its parent', async () => {
    const wrapper = await mountSidebar()
    await wrapper.get('[aria-label="Chat actions"]').trigger('click')
    await flushPromises()

    const move = Array.from(document.body.querySelectorAll<HTMLElement>('button'))
      .find(button => button.textContent?.trim() === 'Move to...')!
    move.focus()
    move.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }))
    await flushPromises()

    const menus = document.body.querySelectorAll('[data-reka-menu-content]')
    expect(menus.length).toBe(2)
    expect(menus[1].textContent).toContain('Archive')

    const back = Array.from(document.body.querySelectorAll<HTMLElement>('button'))
      .find(button => button.textContent?.includes('Back'))!
    back.click()
    await flushPromises()
    expect(document.body.querySelectorAll('[data-reka-menu-content]')).toHaveLength(1)
    wrapper.unmount()
  })

  it('does not tag the project row with its branch', async () => {
    const store = useProjectStore()
    store.projects[0].repo = { path: '/r/app', branch: 'main', dirty: true, missing: false }
    const wrapper = await mountSidebar()
    expect(wrapper.find('.project-repo-tag').exists()).toBe(false)
    expect(wrapper.get('.project-header').text()).not.toContain('main')
    wrapper.unmount()
  })

  it('tags a chat only when it worked outside the main checkout', async () => {
    const store = useProjectStore()
    store.projects[0].repo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
    store.chats[0].repo_location = { path: '/r/app', branch: 'main', is_worktree: false, exists: true }
    store.chats.push({
      ...store.chats[0],
      chat_id: 'chat-wt',
      title: 'In a worktree',
      repo_location: { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true },
    })
    const wrapper = await mountSidebar()
    await flushPromises()
    const tags = wrapper.findAll('.chat-repo-tag')
    expect(tags).toHaveLength(1)
    expect(tags[0].text()).toBe('⑂ feat-x')
    expect(tags[0].attributes('title')).toBe('Worktree: /r/app-wt (code mode off)')
    wrapper.unmount()
  })

  it('tags a code-mode chat with a quiet code marker', async () => {
    const store = useProjectStore()
    store.projects[0].repo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
    store.chats[0].code_mode = true
    const wrapper = await mountSidebar()
    await flushPromises()
    expect(wrapper.findAll('.chat-code-tag')).toHaveLength(1)
    expect(wrapper.get('.chat-code-tag').text()).toBe('code')
    wrapper.unmount()

    store.projects[0].repo = null
    const unlinked = await mountSidebar()
    await flushPromises()
    expect(unlinked.find('.chat-code-tag').exists()).toBe(false)
    unlinked.unmount()
  })

  function worktreeChat(codeMode: boolean) {
    const store = useProjectStore()
    store.projects[0].repo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
    store.chats[0].code_mode = codeMode
    store.chats[0].repo_location = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }
  }

  it('shows only the code badge when code mode and a worktree both apply', async () => {
    worktreeChat(true)
    const wrapper = await mountSidebar()
    await flushPromises()
    const code = wrapper.get('.chat-code-tag')
    expect(code.text()).toBe('code')
    expect(code.attributes('title')).toContain('feat-x')
    expect(code.attributes('title')).toContain('/r/app-wt')
    expect(code.attributes('aria-label')).toContain('feat-x')
    expect(code.attributes('aria-label')).toContain('/r/app-wt')
    expect(wrapper.find('.chat-repo-tag').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the dimmed branch tag when code mode is off', async () => {
    worktreeChat(false)
    const wrapper = await mountSidebar()
    await flushPromises()
    const chip = wrapper.get('.chat-repo-tag')
    expect(chip.text()).toBe('⑂ feat-x')
    expect(chip.classes()).toContain('chat-repo-tag--code-off')
    expect(wrapper.find('.chat-code-tag').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the repository facts in the row title, where a hidden badge cannot hide them', async () => {
    worktreeChat(true)
    const wrapper = await mountSidebar()
    await flushPromises()
    const badge = wrapper.get('.chat-code-tag')
    // A fine pointer hides the row's marks while its actions show, so the
    // facts the badge carries live in the row's own title too.
    expect(wrapper.find('.chat-code-tag').exists()).toBe(true)
    expect(wrapper.get('.chat-item').attributes('title'))
      .toBe(`Menu chat\nDrag to move to another project\n${badge.attributes('title')}`)
    expect(wrapper.get('.chat-item').attributes('title')).toContain('feat-x')
    expect(wrapper.get('.chat-item').attributes('title')).toContain('/r/app-wt')
    wrapper.unmount()
  })

  it('dims the branch chip of a chat whose code mode is off', async () => {
    worktreeChat(false)
    const wrapper = await mountSidebar()
    await flushPromises()
    expect(wrapper.get('.chat-repo-tag').classes()).toContain('chat-repo-tag--code-off')
    wrapper.unmount()
  })

  it('rolls background runs up onto a collapsed project header', async () => {
    const store = useProjectStore()
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'chat' },
      global: { plugins: [router] },
    })
    await nextTick()
    const projectId = store.chats[0].project_id
    const group = () => wrapper.findAll('.project-group').find(g => g.find('.project-name').text().includes(
      store.projects.find(p => p.project_id === projectId)!.name,
    ))!
    const chevron = () => group().get('.project-icon')
    store.backgroundRuns = {
      [store.chats[0].chat_id]: [{ run_id: 'r1', label: '', cmd: ['uv', 'sync'], started_at: '2026-10-06T10:00:00Z', status: 'running', exit_code: null }],
    }
    if (chevron().attributes('aria-expanded') !== 'true') await chevron().trigger('click')
    expect(group().find('.project-run-rollup').exists()).toBe(false)
    await chevron().trigger('click')
    const rollup = group().get('.project-run-rollup')
    expect(rollup.attributes('aria-label')).toBe('1 background run in this project')
    expect(rollup.find('.activity-glyph--run').exists()).toBe(true)
    // Only the name truncates; a long name must not push the mark out of view.
    expect(group().get('.project-name-text').find('.project-run-rollup').exists()).toBe(false)
    wrapper.unmount()
  })
})
