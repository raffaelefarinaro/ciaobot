// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import type { Schedule } from '../../lib/types'
import { useTaskStore } from '../../stores/tasks'
import { useProjectStore } from '../../stores/projects'
import { useWebhookStore } from '../../stores/webhooks'

const confirmMock = vi.fn(async (_message: string) => true)
vi.mock('../../lib/confirm', () => ({ askConfirm: (message: string) => confirmMock(message), pendingConfirm: { value: null } }))

const Stub = defineComponent({ render: () => h('div') })
const ModelSelectorStub = defineComponent({
  props: ['modelValue', 'sections', 'placeholder', 'emptyPlaceholder'],
  render() { return h('div') },
})

function makeSchedule(overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: 'sched-1',
    daily_time_utc: '00:30',
    prompt: 'Sprint review and planning.',
    chat_id: 0,
    created_at: '2026-01-01T00:00:00Z',
    timezone_name: 'Europe/Zurich',
    last_triggered_on: '',
    days_of_week: ['sat'],
    thread_id: null,
    context_label: 'General',
    frequency: 'weekly',
    day_of_month: null,
    run_at_date: null,
    web_chat_id: null,
    web_project_id: 'proj-1',
    workspace: 'work',
    model: 'opus',
    provider: 'claude',
    next_run: '2026-08-15T00:30:00Z',
    last_expected_run: null,
    missed: false,
    enabled: true,
    archive_policy: 'auto',
    title: 'Sprint review and planning',
    ...overrides,
  } as Schedule
}

/** A chat-owned loop: interval cadence bound to, and owned by, one chat. */
function loopSchedule(overrides: Partial<Schedule> = {}): Schedule {
  return makeSchedule({
    title: 'Watch the build',
    frequency: 'interval',
    interval_minutes: 5,
    daily_time_utc: '',
    web_project_id: null,
    web_chat_id: 'chat-a',
    owner_chat_id: 'chat-a',
    expires_at: '2099-01-01T00:00:00Z',
    max_runs: 10,
    run_count: 3,
    context_label: 'Build chat',
    ...overrides,
  })
}

/** Cards are addressed by their uppercase heading, e.g. "Schedule". */
function card(wrapper: VueWrapper, name: string) {
  const found = wrapper.findAll('.prop-card').find(
    section => section.find('.prop-card-name').text() === name,
  )
  if (!found) throw new Error(`no "${name}" card rendered`)
  return found
}

// The panel listens for Escape on `document`, so a wrapper left mounted would
// keep reacting to key events fired by later tests.
const mounted: VueWrapper[] = []

async function mountPanel(schedule = makeSchedule()): Promise<VueWrapper> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/chat/:chatId?', name: 'chat-detail', component: Stub },
      { path: '/schedules/:scheduleId?', name: 'schedules', component: Stub },
    ],
  })
  await router.push(`/schedules/${schedule.schedule_id}`)
  await router.isReady()

  const store = useTaskStore()
  store.schedules = [schedule]
  store.schedulesLoaded = true
  store.scheduleLoadError = ''
  store.fetchSchedules = vi.fn(async () => {})
  store.fetchModels = vi.fn(async () => ({ status: 'success' as const, workspace: 'work', generation: 0 }))
  store.updateSchedule = vi.fn(async () => schedule)

  const projectStore = useProjectStore()
  projectStore.activeWorkspace = 'work'

  const { default: SchedulePanel } = await import('../SchedulePanel.vue')
  const wrapper = mount(SchedulePanel, {
    global: { plugins: [router], stubs: { ModelSelector: ModelSelectorStub, PaneHeader: false } },
  })
  await flushPromises()
  mounted.push(wrapper)
  return wrapper
}

describe('SchedulePanel property cards', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    confirmMock.mockClear()
    confirmMock.mockResolvedValue(true)
  })

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
  })

  it('shows the schedule command in the read view', async () => {
    const wrapper = await mountPanel(makeSchedule({ command: './scripts/sync.sh | tee log' }))
    expect(wrapper.find('.full-command').text()).toBe('./scripts/sync.sh | tee log')
  })

  it('shows no command block when the schedule has none', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('.full-command').exists()).toBe(false)
  })

  it('groups the properties into When, Where it runs, Model and Results sections', async () => {
    const wrapper = await mountPanel()
    const names = wrapper.findAll('.prop-card-name').map(n => n.text())
    expect(names).toEqual(['When', 'Where it runs', 'Model', 'Results'])
  })

  it('keeps the other cards readable while one card is being edited', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'When').find('.card-edit').trigger('click')

    expect(card(wrapper, 'When').find('.card-form').exists()).toBe(true)
    // The untouched cards still render their values, not inputs.
    expect(card(wrapper, 'Where it runs').find('.card-form').exists()).toBe(false)
    expect(card(wrapper, 'Where it runs').find('.prop-rows').exists()).toBe(true)
    expect(card(wrapper, 'Model').find('.prop-rows').exists()).toBe(true)
  })

  it('always offers a visible Cancel inside the card being edited', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Model').find('.card-edit').trigger('click')

    const actions = card(wrapper, 'Model').find('.card-actions')
    expect(actions.exists()).toBe(true)
    expect(actions.text()).toContain('Cancel')
  })

  it('keeps archive behavior editable in the Advanced card', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Results').find('.card-edit').trigger('click')

    await card(wrapper, 'Results').find('select').setValue('manual')
    expect(card(wrapper, 'Results').find('.card-actions .btn-primary').attributes('disabled')).toBeUndefined()
    await card(wrapper, 'Results').find('.card-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(useTaskStore().updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({ archive_policy: 'manual' }),
    )
  })

  it('keeps the header actions in place while editing', async () => {
    const wrapper = await mountPanel()
    const headerText = () => wrapper.findAll('.btn-small').map(b => b.text()).join(' ')
    expect(headerText()).toContain('Run now')

    await card(wrapper, 'When').find('.card-edit').trigger('click')
    expect(headerText()).toContain('Run now')
    expect(headerText()).toContain('Pause')
  })

  it('disables Save until something actually changes, then enables it', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'When').find('.card-edit').trigger('click')

    const save = () => card(wrapper, 'When').find('.card-actions .btn-primary')
    expect(save().attributes('disabled')).toBeDefined()
    expect(card(wrapper, 'When').find('.dirty-flag').exists()).toBe(false)

    await card(wrapper, 'When').find('input[type="time"]').setValue('07:45')
    expect(save().attributes('disabled')).toBeUndefined()
    expect(card(wrapper, 'When').find('.dirty-flag').exists()).toBe(true)
  })

  it('leaves edit mode on Escape without confirming when nothing changed', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Where it runs').find('.card-edit').trigger('click')
    expect(card(wrapper, 'Where it runs').find('.card-form').exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()

    expect(confirmMock).not.toHaveBeenCalled()
    expect(card(wrapper, 'Where it runs').find('.card-form').exists()).toBe(false)
  })

  it('confirms before discarding unsaved changes on Escape', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'When').find('.card-edit').trigger('click')
    await card(wrapper, 'When').find('input[type="time"]').setValue('07:45')

    confirmMock.mockResolvedValue(false)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()

    expect(confirmMock).toHaveBeenCalled()
    // Declining the confirm keeps the user in the form with their edit intact.
    expect(card(wrapper, 'When').find('.card-form').exists()).toBe(true)

    confirmMock.mockResolvedValue(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(card(wrapper, 'When').find('.card-form').exists()).toBe(false)
  })

  it('saves only through the edited card and closes it', async () => {
    const wrapper = await mountPanel()
    const store = useTaskStore()

    await card(wrapper, 'When').find('.card-edit').trigger('click')
    await card(wrapper, 'When').find('input[type="time"]').setValue('07:45')
    await card(wrapper, 'When').find('.card-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(store.updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({ time: '07:45', prompt: 'Sprint review and planning.' }),
    )
    expect(card(wrapper, 'When').find('.card-form').exists()).toBe(false)
  })

  it('withdraws the other edit entry points while one card is open', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.findAll('.card-edit')).toHaveLength(4)

    await card(wrapper, 'When').find('.card-edit').trigger('click')

    // Opening another card would re-seed editData and silently drop the edits
    // in progress, so no other card offers an entry point until this one closes.
    expect(wrapper.findAll('.card-edit')).toHaveLength(0)
    expect(wrapper.find('.prompt-actions').text()).not.toContain('Edit')

    await card(wrapper, 'When').find('.card-actions .btn-chip').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.card-edit')).toHaveLength(4)
  })

  it('limits system routines to the Engine and Advanced edit cards', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system' }))
    // Title, cadence, and delivery target are fixed for a system routine;
    // only the engine override and archive behavior are editable.
    expect(card(wrapper, 'When').find('.card-edit').exists()).toBe(false)
    expect(card(wrapper, 'Where it runs').find('.card-edit').exists()).toBe(false)
    expect(card(wrapper, 'Model').find('.card-edit').exists()).toBe(true)
    expect(card(wrapper, 'Results').find('.card-edit').exists()).toBe(true)
    // …but the system workspace switcher is still reachable.
    expect(card(wrapper, 'Where it runs').find('.system-workspace-control').exists()).toBe(true)
  })

  it('does not save a system routine move when the workspace guard declines', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system', workspace: 'work' }))
    const projects = useProjectStore()
    projects.workspaces = [{ name: 'work' }, { name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    projects.registerWorkspaceSwitchGuard(async () => false)
    const select = wrapper.get<HTMLSelectElement>('.system-workspace-control select')
    await select.setValue('personal')
    await flushPromises()
    expect(useTaskStore().updateSchedule).not.toHaveBeenCalled()
    expect(projects.activeWorkspace).toBe('work')
    expect(select.element.value).toBe('work')
    expect(useTaskStore().schedules[0]).toMatchObject({ workspace: 'work', model: 'opus', provider: 'claude' })
  })

  it('does not save a system routine without an override when the workspace guard declines', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system', model: '', provider: '' }))
    const projects = useProjectStore()
    projects.workspaces = [{ name: 'work' }, { name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    projects.registerWorkspaceSwitchGuard(async () => false)
    const select = wrapper.get<HTMLSelectElement>('.system-workspace-control select')
    await select.setValue('personal')
    await flushPromises()
    expect(useTaskStore().updateSchedule).not.toHaveBeenCalled()
    expect(select.element.value).toBe('work')
    expect(projects.activeWorkspace).toBe('work')
    expect(confirmMock).not.toHaveBeenCalled()
  })

  it('reports a failed system routine save after switching without changing the saved routine', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system', workspace: 'work' }))
    const projects = useProjectStore()
    projects.workspaces = [{ name: 'work' }, { name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    let reject!: (error: Error) => void
    vi.mocked(useTaskStore().updateSchedule).mockReturnValueOnce(new Promise((_, fail) => { reject = fail }))
    const select = wrapper.get<HTMLSelectElement>('.system-workspace-control select')
    await select.setValue('personal')
    await flushPromises()
    expect(projects.activeWorkspace).toBe('personal')
    expect(select.attributes('disabled')).toBeDefined()
    expect(wrapper.get('[role="status"]').text()).toContain('Moving routine')
    expect(useTaskStore().updateSchedule).toHaveBeenCalledWith('sched-1', { workspace: 'personal', model: '', provider: '' }, expect.any(Symbol))
    reject(new Error('offline'))
    await flushPromises()
    expect(wrapper.get('[role="alert"]').text()).toContain('offline')
    expect(select.element.value).toBe('work')
    expect(useTaskStore().schedules[0]).toMatchObject({ workspace: 'work', model: 'opus', provider: 'claude' })
  })

  it('switches before saving a system routine without a model override', async () => {
    const schedule = makeSchedule({ scope: 'system', workspace: 'work', model: '', provider: '' })
    const wrapper = await mountPanel(schedule)
    const projects = useProjectStore()
    projects.workspaces = [{ name: 'work' }, { name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    const store = useTaskStore()
    vi.mocked(store.updateSchedule).mockImplementationOnce(async (_id, updates) => {
      expect(projects.activeWorkspace).toBe('personal')
      const saved = { ...schedule, ...updates } as Schedule
      store.schedules = [saved]
      return saved
    })
    await wrapper.get('.system-workspace-control select').setValue('personal')
    await flushPromises()
    expect(store.updateSchedule).toHaveBeenCalledWith('sched-1', { workspace: 'personal' }, expect.any(Symbol))
    expect(projects.activeWorkspace).toBe('personal')
    expect(confirmMock).not.toHaveBeenCalled()
  })

  it('does not save if the workspace changes while a system routine guard is pending', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system', workspace: 'work' }))
    const projects = useProjectStore()
    projects.workspaces = [{ name: 'work' }, { name: 'personal' }] as typeof projects.workspaces
    projects.workspaceRegistryStatus = 'ready'
    let allow!: (value: boolean) => void
    projects.registerWorkspaceSwitchGuard(() => new Promise(resolve => { allow = resolve }))
    const select = wrapper.get<HTMLSelectElement>('.system-workspace-control select')
    await select.setValue('personal')
    await flushPromises()
    expect(select.attributes('disabled')).toBeDefined()
    projects.activeWorkspace = 'personal'
    projects.activeWorkspace = 'work'
    allow(true)
    await flushPromises()
    expect(useTaskStore().updateSchedule).not.toHaveBeenCalled()
    expect(select.element.value).toBe('work')
  })

  it.each(['system', 'user'] as const)('does not force the workspace back when the same %s schedule is refreshed', async scope => {
    const wrapper = await mountPanel(makeSchedule({ scope, workspace: 'work' }))
    const projects = useProjectStore()
    projects.activeWorkspace = 'personal'
    const store = useTaskStore()
    store.schedules = [{ ...store.schedules[0], title: 'Refreshed' }]
    await flushPromises()
    expect(projects.activeWorkspace).toBe('personal')
    expect(wrapper.text()).toContain('Refreshed')
  })

  it('loads a cold model catalog when editing a system routine and gives it to the picker', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system' }))
    const store = useTaskStore()
    const models = {
      models: ['claude-sonnet-4-5'],
      default: 'claude-sonnet-4-5',
      provider_models: { claude: ['claude-sonnet-4-5'] },
      provider_defaults: { claude: 'claude-sonnet-4-5' },
    }
    store.fetchModels = vi.fn(async () => {
      store.models = models
      return { status: 'success' as const, workspace: 'work', generation: 0 }
    })

    await card(wrapper, 'Model').find('.card-edit').trigger('click')
    await flushPromises()

    expect(store.fetchModels).toHaveBeenCalledOnce()
    const selector = wrapper.findComponent(ModelSelectorStub)
    expect(selector.exists()).toBe(true)
    expect(selector.props('sections')).toEqual([
      expect.objectContaining({ models: ['claude-sonnet-4-5'] }),
    ])
  })

  it('fetches a cold model catalog for an ordinary schedule Model edit', async () => {
    const wrapper = await mountPanel()
    const store = useTaskStore()
    store.fetchModels = vi.fn(async () => ({ status: 'success' as const, workspace: 'work', generation: 0 }))

    await card(wrapper, 'Model').find('.card-edit').trigger('click')

    expect(store.fetchModels).toHaveBeenCalledOnce()
  })

  it('reuses a cached model catalog for a Model edit', async () => {
    const wrapper = await mountPanel()
    const store = useTaskStore()
    store.models = {
      models: ['claude-sonnet-4-5'],
      default: 'claude-sonnet-4-5',
      provider_models: { claude: ['claude-sonnet-4-5'] },
      provider_defaults: { claude: 'claude-sonnet-4-5' },
    }
    store.fetchModels = vi.fn(async () => ({ status: 'success' as const, workspace: 'work', generation: 0 }))

    await card(wrapper, 'Model').find('.card-edit').trigger('click')

    expect(store.fetchModels).not.toHaveBeenCalled()
  })

  it('does not fetch models for a chat-bound interval Model edit', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval',
      interval_minutes: 10,
      web_chat_id: 'chat-1',
      web_project_id: null,
    }))
    const store = useTaskStore()
    store.fetchModels = vi.fn(async () => ({ status: 'success' as const, workspace: 'work', generation: 0 }))

    await card(wrapper, 'Model').find('.card-edit').trigger('click')

    expect(store.fetchModels).not.toHaveBeenCalled()
  })
  // ── Interval cadence (the primitive that replaced loops) ──

  it('shows the cadence and a status row instead of a time of day', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval',
      interval_minutes: 15,
      daily_time_utc: '',
      web_chat_id: 'chat-1',
      web_project_id: null,
      last_status: 'busy',
      next_run: '2020-01-01T00:00:00Z',
    }))
    const scheduleCard = card(wrapper, 'When')
    const rows = scheduleCard.findAll('.prop-row').map(r => r.text())
    expect(rows.some(r => r.includes('Every 15 min'))).toBe(true)
    // No time of day: interval cadence has none, and an empty "At" row read as
    // a schedule with a broken time.
    expect(rows.some(r => r.startsWith('At'))).toBe(false)
    // last_status is the health report; the missed-run check cannot speak for
    // a relative cadence.
    expect(rows.some(r => r.includes('waiting — chat busy'))).toBe(true)
    // An overdue interval is not rendered as an absolute past time.
    expect(rows.some(r => r.includes('when the chat is free'))).toBe(true)
  })

  it('shows a status row for a wall-clock entry whose last dispatch failed', async () => {
    // A dispatch failure stamps last_status on the row (issue #407). The
    // missed badge says a slot went unserved; this row says why, and without
    // it the failure sat in the job log with nothing visible anywhere.
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'daily',
      daily_time_utc: '08:00',
      last_status: 'error',
    }))
    const rows = card(wrapper, 'When').findAll('.prop-row').map(r => r.text())
    expect(rows.some(r => r.includes('last run failed'))).toBe(true)
  })

  it('shows a retrying status row and state for a wall-clock entry', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'daily',
      daily_time_utc: '08:00',
      last_status: 'retrying',
    }))
    const rows = card(wrapper, 'When').findAll('.prop-row').map(r => r.text())
    expect(rows.some(r => r.includes('retrying — a service was unreachable'))).toBe(true)
    expect(wrapper.text()).toContain('Retrying')
  })

  it('hides the status row for a healthy wall-clock entry', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'daily',
      daily_time_utc: '08:00',
      last_status: '',
    }))
    const rows = card(wrapper, 'When').findAll('.prop-row').map(r => r.text())
    expect(rows.some(r => r.includes('last run failed'))).toBe(false)
  })

  it('reads the model off the target chat for a chat-bound interval entry', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval',
      interval_minutes: 10,
      web_chat_id: 'chat-1',
      web_project_id: null,
      model: 'opus',
      effective_model: 'sonnet',
    }))
    // A stored model is ignored at dispatch, so it must not be presented as
    // the active override.
    expect(card(wrapper, 'Model').text()).toContain('sonnet (from the target chat)')
    expect(card(wrapper, 'Model').text()).not.toContain('override')
  })

  it('says Stop rather than Disable for an interval entry', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval',
      interval_minutes: 10,
      web_chat_id: 'chat-1',
      web_project_id: null,
    }))
    expect(wrapper.text()).toContain('Stop')
    expect(wrapper.text()).not.toContain('Disable')
  })

  it('saves an edited cadence and clears the time of day', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval',
      interval_minutes: 10,
      daily_time_utc: '',
      web_chat_id: 'chat-1',
      web_project_id: null,
    }))
    const store = useTaskStore()
    await card(wrapper, 'When').find('.card-edit').trigger('click')
    const minutes = card(wrapper, 'When').find('input[type="number"]')
    await minutes.setValue(45)
    await card(wrapper, 'When').find('.card-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(store.updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({
        frequency: 'interval',
        interval_minutes: 45,
        time: '',
      }),
    )
  })

  // An interval entry has no daily_time_utc, so switching one to a wall-clock
  // cadence arrives with the time field empty. Saving that stored an
  // automation that reads as enabled and can never fire.
  it('blocks Save when an interval entry is switched to a cadence needing a time', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval', interval_minutes: 5, daily_time_utc: '',
      days_of_week: null, web_chat_id: 'chat-1', web_project_id: null,
    }))
    await card(wrapper, 'When').find('.card-edit').trigger('click')

    await card(wrapper, 'When').find('select').setValue('daily')

    const save = card(wrapper, 'When').find('.card-actions .btn-primary')
    expect(save.attributes('disabled')).toBeDefined()
    expect(card(wrapper, 'When').text()).toContain('Pick a time')
    expect(useTaskStore().updateSchedule).not.toHaveBeenCalled()
  })

  it('re-enables Save once a time is supplied', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval', interval_minutes: 5, daily_time_utc: '',
      days_of_week: null, web_chat_id: 'chat-1', web_project_id: null,
    }))
    await card(wrapper, 'When').find('.card-edit').trigger('click')
    await card(wrapper, 'When').find('select').setValue('daily')
    await card(wrapper, 'When').find('input[type="time"]').setValue('09:30')

    const save = card(wrapper, 'When').find('.card-actions .btn-primary')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flushPromises()

    expect(useTaskStore().updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({ frequency: 'daily', time: '09:30' }),
    )
  })

  it('keeps Delete out of the header and behind the actions menu', async () => {
    const wrapper = await mountPanel()
    const store = useTaskStore()
    store.deleteSchedule = vi.fn(async () => {})
    const headerText = wrapper.findAll('.btn-small').map(b => b.text()).join(' ')
    expect(headerText).not.toContain('Delete')

    await wrapper.get('button[aria-label="More automation actions"]').trigger('click')
    await flushPromises()
    const del = Array.from(document.body.querySelectorAll<HTMLButtonElement>('.item-header-menu button'))
      .find(button => button.textContent?.includes('Delete'))
    expect(del).toBeTruthy()
    del!.click()
    await flushPromises()

    expect(confirmMock).toHaveBeenCalledWith('Delete this automation? It will stop running.')
    expect(store.deleteSchedule).toHaveBeenCalledWith('sched-1')
  })

  it('draws a chat loop with its own rows, no edit, run or pause controls, and one Stop', async () => {
    const wrapper = await mountPanel(loopSchedule())
    for (const name of ['When', 'Where it runs', 'Model']) {
      expect(card(wrapper, name).find('.card-edit').exists()).toBe(false)
    }
    expect(wrapper.find('.header-run').exists()).toBe(false)
    expect(wrapper.find('.desktop-only').exists()).toBe(false)
    expect(wrapper.get('a[href="/chat/chat-a"]').text()).toContain('Build chat')
    const when = card(wrapper, 'When').text()
    expect(when).toContain('Runs left')
    expect(when).toContain('7')
    expect(when).toContain('Ends')

    // The prompt is still editable: a loop's prompt is not fixed like its lifetime.
    expect(wrapper.findAll('.text-link').some(b => b.text() === 'Edit')).toBe(true)

    // No ⋯ menu for a loop: its only actions are Stop and the prompt. Delete is Stop.
    expect(wrapper.find('button[aria-label="More automation actions"]').exists()).toBe(false)

    const stop = wrapper.findAll('button').find(b => b.text() === 'Stop loop')
    expect(stop).toBeTruthy()
    const store = useTaskStore()
    store.deleteSchedule = vi.fn(async () => {})
    await stop!.trigger('click')
    await flushPromises()
    expect(confirmMock).toHaveBeenCalledWith('Stop this loop? It won\'t run again.')
    expect(store.deleteSchedule).toHaveBeenCalledTimes(1)
    expect(store.deleteSchedule).toHaveBeenCalledWith('sched-1')
  })

  it('does not delete a loop when the Stop confirm is declined', async () => {
    confirmMock.mockResolvedValue(false)
    const wrapper = await mountPanel(loopSchedule())
    const store = useTaskStore()
    store.deleteSchedule = vi.fn(async () => {})
    await wrapper.findAll('button').find(b => b.text() === 'Stop loop')!.trigger('click')
    await flushPromises()
    expect(store.deleteSchedule).not.toHaveBeenCalled()
  })

  it('shows the automation\'s own state in the rail, and flags a failed run', async () => {
    const wrapper = await mountPanel(makeSchedule({
      last_status: 'error',
      last_dispatched_at: '2026-08-14T00:30:00Z',
      last_run_chat_id: 'chat-9',
    }))
    const rail = wrapper.get('.page-rail')
    expect(rail.text()).toContain('Status')
    expect(rail.get('.rail-attention').text()).toBe('Last run failed')
    expect(rail.find('a[href="/chat/chat-9"]').exists()).toBe(true)
    // No per-routine totals: the run log does not record routine runs.
    expect(rail.text()).not.toContain('Success rate')
  })
})

describe('SchedulePanel Automations pages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    while (mounted.length) mounted.pop()?.unmount()
  })

  async function mountPage(schedules: Schedule[], path = '/schedules', opts: { loaded?: boolean } = {}) {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: Stub },
        { path: '/chat/:chatId?', component: Stub },
        { path: '/schedules/:scheduleId?', component: Stub },
        { path: '/automations/loops', component: Stub },
        { path: '/automations/webhooks', component: Stub },
      ],
    })
    await router.push(path)
    await router.isReady()
    const store = useTaskStore()
    store.schedules = schedules
    store.schedulesLoaded = opts.loaded ?? true
    store.scheduleLoadError = ''
    store.fetchSchedules = vi.fn(async () => {})
    store.fetchModels = vi.fn(async () => ({ status: 'success' as const, workspace: 'work', generation: 0 }))
    useProjectStore().activeWorkspace = 'work'
    // The webhook section reads its own store on mount. Seeded as already read so
    // this file never reaches the network for it: the section's five load states
    // are pinned in `WebhookTriggers.test.ts`.
    const webhooks = useWebhookStore()
    webhooks.loadedWorkspace = 'work'
    webhooks.triggers = []
    const { default: SchedulePanel } = await import('../SchedulePanel.vue')
    const wrapper = mount(SchedulePanel, {
      attachTo: document.body,
      global: { plugins: [router], stubs: { ModelSelector: Stub, PaneHeader: false } },
    })
    await flushPromises()
    mounted.push(wrapper)
    return { wrapper, router }
  }

  const rowTitles = (section: ReturnType<VueWrapper['get']>) =>
    section.findAll('.automation-row .list-row-title').map(t => t.text())

  it('lists system routines first, then the user\'s by next run with one-offs among them and paused ones last; loops elsewhere', async () => {
    useProjectStore().chats = [{ chat_id: 'chat-a', title: 'Build chat' }] as never
    const { wrapper } = await mountPage([
      makeSchedule({ schedule_id: 'late', title: 'Late brief', next_run: '2026-08-15T10:00:00Z' }),
      makeSchedule({ schedule_id: 'old', title: 'Old sweep', enabled: false, next_run: null }),
      makeSchedule({ schedule_id: 'care', title: 'Workspace care', scope: 'system' }),
      makeSchedule({ schedule_id: 'once', title: 'Call the bank', frequency: 'once', run_at_date: '2026-08-15', next_run: '2026-08-15T08:00:00Z' }),
      makeSchedule({ schedule_id: 'soon', title: 'Morning brief', next_run: '2026-08-15T07:00:00Z' }),
      loopSchedule({ schedule_id: 'l' }),
    ])
    const sections = wrapper.findAll('section.ov-section').map(s => s.attributes('aria-labelledby'))
    expect(sections).toEqual(['ov-system-title', 'ov-routines-title'])
    expect(wrapper.get('#ov-system-title').text()).toBe('System routines')
    const system = wrapper.get('[aria-labelledby="ov-system-title"]')
    expect(system.text()).toContain("Ciaobot's own upkeep. They run on their own and never need you.")
    expect(rowTitles(system)).toEqual(['Workspace care'])
    const yours = wrapper.get('[aria-labelledby="ov-routines-title"]')
    expect(rowTitles(yours)).toEqual(['Morning brief', 'Call the bank', 'Late brief', 'Old sweep'])
    const rows = yours.findAll('.automation-row')
    expect(rows[1].get('.automation-tag').text()).toBe('once')
    expect(rows[3].classes()).toContain('automation-row--paused')
    expect(rows[3].get('.list-row-end').text()).toBe('paused')
    expect(rows[0].attributes('href')).toBe('/schedules/soon')
    // No loop on this page, and none of the old overview's sections or counts.
    expect(wrapper.text()).not.toContain('Watch the build')
    expect(wrapper.text()).not.toContain('In this workspace')
    expect(wrapper.text()).not.toContain('Next up')
  })

  it('marks a missed routine on its row and keeps a short Missed notice with Run all', async () => {
    const { wrapper } = await mountPage([
      makeSchedule({ schedule_id: 'm', title: 'Morning brief', missed: true, last_expected_run: '2026-08-14T07:00:00Z' }),
      makeSchedule({ schedule_id: 'ok', title: 'Quiet one', last_status: 'ok', last_dispatched_at: '2026-08-14T06:00:00Z' }),
    ])
    const notice = wrapper.get('[aria-labelledby="ov-missed-title"]')
    expect(notice.text()).toContain('Morning brief did not run when it was due.')
    expect(notice.get('button').text()).toBe('Run all')
    const row = wrapper.findAll('.automation-row').find(r => r.text().includes('Morning brief'))!
    expect(row.find('.activity-glyph--alert').exists()).toBe(true)
    expect(row.get('.automation-state-word').text()).toBe('missed')
    expect(row.get('.automation-state .sr-only').text()).toBe('Missed a run.')
    const quiet = wrapper.findAll('.automation-row').find(r => r.text().includes('Quiet one'))!
    expect(quiet.find('.care-glyph--done').exists()).toBe(true)
    expect(quiet.find('.automation-state-word').exists()).toBe(false)
  })

  it('shows a run in progress as the working glyph, and a failed run as the alert and a word', async () => {
    const { wrapper } = await mountPage([
      makeSchedule({ schedule_id: 'r', title: 'Running one', last_status: 'running', last_dispatched_at: '2026-08-14T06:00:00Z', last_run_chat_id: 'chat-run' }),
      makeSchedule({ schedule_id: 'e', title: 'Flaky one', last_status: 'error', last_dispatched_at: '2026-08-14T05:00:00Z' }),
    ])
    const row = (title: string) => wrapper.findAll('.automation-row').find(r => r.text().includes(title))!
    expect(row('Running one').find('.activity-glyph--working').exists()).toBe(true)
    expect(row('Running one').get('.automation-state').attributes('title')).toBe('Running now')
    expect(row('Flaky one').find('.activity-glyph--alert').exists()).toBe(true)
    expect(row('Flaky one').get('.automation-state-word').text()).toBe('failed')
  })

  it('docks Recent runs on the right: newest first, five at most, each opening its chat', async () => {
    const runs = Array.from({ length: 6 }, (_, i) => makeSchedule({
      schedule_id: `s${i}`,
      title: `Routine ${i}`,
      last_dispatched_at: `2026-08-1${i}T08:00:00Z`,
      last_run_chat_id: `chat-${i}`,
    }))
    const { wrapper } = await mountPage(runs)
    const rail = wrapper.get('.page-rail .docked-window')
    expect(rail.get('.docked-window-head h2').text()).toBe('Recent runs')
    const links = rail.findAll('a.docked-row')
    expect(links.map(l => l.get('.docked-row-title').text())).toEqual(['Routine 5', 'Routine 4', 'Routine 3', 'Routine 2', 'Routine 1'])
    expect(links[0].attributes('href')).toBe('/chat/chat-5')
  })

  it('shows the last run in the browser zone, not the automation\'s UTC clock', async () => {
    // Pinned so the expected clock is the same on every machine: 00:01 UTC on a
    // Saturday is 09:01 in Tokyo, which has no DST.
    const previousTz = process.env.TZ
    process.env.TZ = 'Asia/Tokyo'
    try {
      const { wrapper } = await mountPage([
        makeSchedule({
          schedule_id: 'utc',
          title: 'Early sweep',
          timezone_name: 'UTC',
          daily_time_utc: '00:01',
          last_dispatched_at: '2026-12-05T00:01:00Z',
          next_run: '2026-12-06T00:01:00Z',
        }),
      ])
      const end = wrapper.get('.automation-row .list-row-end').text()
      expect(end).toContain('Sat 09:01')
      expect(end).not.toContain('00:01')
    } finally {
      if (previousTz === undefined) delete process.env.TZ
      else process.env.TZ = previousTz
    }
  })

  it('states the cadence in browser time, and where it runs', async () => {
    const previousTz = process.env.TZ
    process.env.TZ = 'Europe/Rome'
    try {
      const { wrapper } = await mountPage([
        makeSchedule({
          schedule_id: 'utc',
          title: 'Early sweep',
          timezone_name: 'UTC',
          daily_time_utc: '00:01',
          frequency: 'daily',
          days_of_week: null,
          next_run: '2026-12-05T00:01:00Z',
        }),
      ])
      const sub = wrapper.get('.automation-row .list-row-sub').text()
      expect(sub).toContain('Every day at 01:01')
      expect(sub).not.toContain('00:01')
      expect(sub).toContain('new chat in General')
    } finally {
      if (previousTz === undefined) delete process.env.TZ
      else process.env.TZ = previousTz
    }
  })

  it('teaches with an empty state and asks ChatLayout for the form from New routine', async () => {
    const { wrapper } = await mountPage([
      makeSchedule({ schedule_id: 'care', title: 'Workspace care', scope: 'system' }),
    ])
    const yours = wrapper.get('[aria-labelledby="ov-routines-title"]')
    expect(yours.text()).toContain('No routines yet')
    await yours.get('button.automations-new').trigger('click')
    expect(wrapper.emitted('new')).toHaveLength(1)
  })

  it('draws the Loops page with each loop\'s chat, cadence, end and next run, and teaches when empty', async () => {
    useProjectStore().chats = [{ chat_id: 'chat-a', title: 'Build chat' }] as never
    const { wrapper } = await mountPage([
      makeSchedule({ schedule_id: 'a', title: 'Morning brief' }),
      loopSchedule({ schedule_id: 'l', next_run: new Date(Date.now() + 2 * 60_000).toISOString() }),
    ], '/automations/loops')
    const section = wrapper.get('[aria-labelledby="ov-loops-title"]')
    expect(section.text()).toContain('A loop runs inside one chat and ends on its own.')
    expect(rowTitles(section)).toEqual(['Watch the build'])
    const row = section.get('.automation-row')
    expect(row.get('.list-row-sub').text()).toMatch(/^In “Build chat” · every 5 min · ends /)
    expect(row.get('.list-row-end').text()).toContain('next in 2m')
    expect(wrapper.text()).not.toContain('Morning brief')

    const empty = await mountPage([makeSchedule({ schedule_id: 'a' })], '/automations/loops')
    expect(empty.wrapper.text()).toContain('No loops running')
  })

  it('draws the Webhooks page without waiting on the automation list', async () => {
    const { wrapper } = await mountPage([], '/automations/webhooks', { loaded: false })
    const webhooks = wrapper.get('[aria-labelledby="wh-section-title"]')
    expect(webhooks.text()).toContain('Webhook triggers')
    expect(webhooks.text()).toContain('New trigger')
    expect(wrapper.find('[aria-label="Loading automations"]').exists()).toBe(false)
  })

  it('starts each page with the three-segment switcher, the current one marked', async () => {
    const { wrapper } = await mountPage([], '/automations/loops')
    const links = wrapper.findAll('.automations-switcher a')
    expect(links.map(l => l.text())).toEqual(['Schedules', 'Loops', 'Webhooks'])
    expect(links.map(l => l.attributes('href'))).toEqual(['/schedules', '/automations/loops', '/automations/webhooks'])
    expect(links.map(l => l.attributes('aria-current') ?? '')).toEqual(['', 'page', ''])
  })

  it('moves focus to the new page\'s heading on a page change, but leaves it on a switcher segment', async () => {
    const { wrapper, router } = await mountPage([makeSchedule({ schedule_id: 'a' })])
    await router.push('/automations/loops')
    await flushPromises()
    expect(document.activeElement?.id).toBe('ov-loops-title')

    wrapper.get<HTMLAnchorElement>('.automations-switcher a[href="/schedules"]').element.focus()
    await router.push('/schedules')
    await flushPromises()
    expect(document.activeElement?.getAttribute('href')).toBe('/schedules')
  })

  it('shows row skeletons, not cards, while the list first loads, with the switcher', async () => {
    const { wrapper } = await mountPage([], '/schedules', { loaded: false })
    expect(wrapper.find('.skeleton--rows').exists()).toBe(true)
    expect(wrapper.find('.automations-switcher').exists()).toBe(true)
  })
})
