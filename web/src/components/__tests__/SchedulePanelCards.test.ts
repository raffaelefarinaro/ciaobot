// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h } from 'vue'
import type { Schedule } from '../../lib/types'
import { useTaskStore } from '../../stores/tasks'
import { useProjectStore } from '../../stores/projects'

const confirmMock = vi.fn(async (_message: string) => true)
vi.mock('../../lib/confirm', () => ({ askConfirm: (message: string) => confirmMock(message) }))

const Stub = defineComponent({ render: () => h('div') })

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
  store.fetchSchedules = vi.fn(async () => {})
  store.fetchModels = vi.fn(async () => {})
  store.updateSchedule = vi.fn(async () => schedule)

  const projectStore = useProjectStore()
  projectStore.activeWorkspace = 'work'

  const { default: SchedulePanel } = await import('../SchedulePanel.vue')
  const wrapper = mount(SchedulePanel, {
    global: { plugins: [router], stubs: { ModelSelector: Stub, PaneHeader: false } },
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

  it('groups the properties into Schedule, Delivery, Engine and Advanced cards', async () => {
    const wrapper = await mountPanel()
    const names = wrapper.findAll('.prop-card-name').map(n => n.text())
    expect(names).toEqual(['Schedule', 'Delivery', 'Engine', 'Advanced'])
  })

  it('keeps the other cards readable while one card is being edited', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')

    expect(card(wrapper, 'Schedule').find('.card-form').exists()).toBe(true)
    // The untouched cards still render their values, not inputs.
    expect(card(wrapper, 'Delivery').find('.card-form').exists()).toBe(false)
    expect(card(wrapper, 'Delivery').find('.prop-rows').exists()).toBe(true)
    expect(card(wrapper, 'Engine').find('.prop-rows').exists()).toBe(true)
  })

  it('always offers a visible Cancel inside the card being edited', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Engine').find('.card-edit').trigger('click')

    const actions = card(wrapper, 'Engine').find('.card-actions')
    expect(actions.exists()).toBe(true)
    expect(actions.text()).toContain('Cancel')
  })

  it('keeps archive behavior editable in the Advanced card', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Advanced').find('.card-edit').trigger('click')

    await card(wrapper, 'Advanced').find('select').setValue('manual')
    expect(card(wrapper, 'Advanced').find('.card-actions .btn-primary').attributes('disabled')).toBeUndefined()
    await card(wrapper, 'Advanced').find('.card-actions .btn-primary').trigger('click')
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

    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')
    expect(headerText()).toContain('Run now')
    expect(headerText()).toContain('Disable')
  })

  it('disables Save until something actually changes, then enables it', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')

    const save = () => card(wrapper, 'Schedule').find('.card-actions .btn-primary')
    expect(save().attributes('disabled')).toBeDefined()
    expect(card(wrapper, 'Schedule').find('.dirty-flag').exists()).toBe(false)

    await card(wrapper, 'Schedule').find('input[type="time"]').setValue('07:45')
    expect(save().attributes('disabled')).toBeUndefined()
    expect(card(wrapper, 'Schedule').find('.dirty-flag').exists()).toBe(true)
  })

  it('leaves edit mode on Escape without confirming when nothing changed', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Delivery').find('.card-edit').trigger('click')
    expect(card(wrapper, 'Delivery').find('.card-form').exists()).toBe(true)

    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()

    expect(confirmMock).not.toHaveBeenCalled()
    expect(card(wrapper, 'Delivery').find('.card-form').exists()).toBe(false)
  })

  it('confirms before discarding unsaved changes on Escape', async () => {
    const wrapper = await mountPanel()
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')
    await card(wrapper, 'Schedule').find('input[type="time"]').setValue('07:45')

    confirmMock.mockResolvedValue(false)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()

    expect(confirmMock).toHaveBeenCalled()
    // Declining the confirm keeps the user in the form with their edit intact.
    expect(card(wrapper, 'Schedule').find('.card-form').exists()).toBe(true)

    confirmMock.mockResolvedValue(true)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await flushPromises()
    expect(card(wrapper, 'Schedule').find('.card-form').exists()).toBe(false)
  })

  it('saves only through the edited card and closes it', async () => {
    const wrapper = await mountPanel()
    const store = useTaskStore()

    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')
    await card(wrapper, 'Schedule').find('input[type="time"]').setValue('07:45')
    await card(wrapper, 'Schedule').find('.card-actions .btn-primary').trigger('click')
    await flushPromises()

    expect(store.updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({ time: '07:45', prompt: 'Sprint review and planning.' }),
    )
    expect(card(wrapper, 'Schedule').find('.card-form').exists()).toBe(false)
  })

  it('withdraws the other edit entry points while one card is open', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.findAll('.card-edit')).toHaveLength(4)

    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')

    // Opening another card would re-seed editData and silently drop the edits
    // in progress, so no other card offers an entry point until this one closes.
    expect(wrapper.findAll('.card-edit')).toHaveLength(0)
    expect(wrapper.find('.prompt-actions').text()).not.toContain('Edit')

    await card(wrapper, 'Schedule').find('.card-actions .btn-chip').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.card-edit')).toHaveLength(4)
  })

  it('hides the per-card edit buttons on system routines', async () => {
    const wrapper = await mountPanel(makeSchedule({ scope: 'system' }))
    expect(wrapper.findAll('.card-edit')).toHaveLength(0)
    // …but the system workspace switcher is still reachable.
    expect(card(wrapper, 'Delivery').find('.system-workspace-control').exists()).toBe(true)
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
    const scheduleCard = card(wrapper, 'Schedule')
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
    expect(card(wrapper, 'Engine').text()).toContain('sonnet (from the target chat)')
    expect(card(wrapper, 'Engine').text()).not.toContain('override')
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
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')
    const minutes = card(wrapper, 'Schedule').find('input[type="number"]')
    await minutes.setValue(45)
    await card(wrapper, 'Schedule').find('.card-actions .btn-primary').trigger('click')
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
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')

    await card(wrapper, 'Schedule').find('select').setValue('daily')

    const save = card(wrapper, 'Schedule').find('.card-actions .btn-primary')
    expect(save.attributes('disabled')).toBeDefined()
    expect(card(wrapper, 'Schedule').text()).toContain('Pick a time')
    expect(useTaskStore().updateSchedule).not.toHaveBeenCalled()
  })

  it('re-enables Save once a time is supplied', async () => {
    const wrapper = await mountPanel(makeSchedule({
      frequency: 'interval', interval_minutes: 5, daily_time_utc: '',
      days_of_week: null, web_chat_id: 'chat-1', web_project_id: null,
    }))
    await card(wrapper, 'Schedule').find('.card-edit').trigger('click')
    await card(wrapper, 'Schedule').find('select').setValue('daily')
    await card(wrapper, 'Schedule').find('input[type="time"]').setValue('09:30')

    const save = card(wrapper, 'Schedule').find('.card-actions .btn-primary')
    expect(save.attributes('disabled')).toBeUndefined()
    await save.trigger('click')
    await flushPromises()

    expect(useTaskStore().updateSchedule).toHaveBeenCalledWith(
      'sched-1',
      expect.objectContaining({ frequency: 'daily', time: '09:30' }),
    )
  })
})
