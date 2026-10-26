// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../lib/api', () => ({
  api: {
    get: vi.fn(() => Promise.resolve([])),
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  },
}))
import { api } from '../lib/api'
import { FOCUS_REFRESH_MIN_MS, useNotificationInboxStore } from './notificationInbox'
import { useProjectStore } from './projects'
import { useTaskSignalsStore } from './taskSignals'
import { useHousekeepingStore } from './housekeeping'
import { useTaskStore } from './tasks'
import projectsSrc from './projects.ts?raw'
import taskSignalsSrc from './taskSignals.ts?raw'
import housekeepingSrc from './housekeeping.ts?raw'
import sourceHealthSrc from '../lib/sourceHealth.ts?raw'
import notificationInboxLibSrc from '../lib/notificationInbox.ts?raw'
import type { SourceHealth } from '../lib/sourceHealth'

const READY_AT = '2026-10-26T10:00:00.000Z'
const ready = (at = READY_AT): SourceHealth => ({ status: 'ready', lastSuccessAt: at, error: '' })
const stale = (at = READY_AT): SourceHealth => ({ status: 'stale', lastSuccessAt: at, error: 'x' })
const unavailable = (): SourceHealth => ({ status: 'unavailable', lastSuccessAt: '', error: 'x' })
const checking = (): SourceHealth => ({ status: 'checking', lastSuccessAt: '', error: '' })

function makeChat(id: string, overrides: Record<string, unknown> = {}) {
  return {
    chat_id: id,
    project_id: 'p-personal',
    title: `Chat ${id}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:00:00Z',
    last_read_at: '2026-10-26T09:00:00Z',
    ...overrides,
  }
}

function setup() {
  const projects = useProjectStore()
  const signals = useTaskSignalsStore()
  const inbox = useNotificationInboxStore()
  projects.projects = [
    { project_id: 'p-personal', name: 'General', workspace: 'personal' },
    { project_id: 'p-work', name: 'General', workspace: 'work' },
  ] as unknown as typeof projects.projects
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  return { projects, signals, inbox }
}

function setChats(projects: ReturnType<typeof useProjectStore>, chats: unknown[]) {
  projects.chats = chats as unknown as typeof projects.chats
}

function markHealthy(projects: ReturnType<typeof useProjectStore>, signals: ReturnType<typeof useTaskSignalsStore>) {
  useHousekeepingStore().actionsHealth = ready()
  useTaskStore().schedulesLoaded = true
  projects.chatsHealth = ready()
  signals.healthWorkspace = projects.activeWorkspace
  signals.health = ready()
}

function deferred() {
  let resolve!: () => void
  const promise = new Promise<void>((r) => { resolve = r })
  return { promise, resolve }
}

describe('notification inbox store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    vi.mocked(api.post).mockClear()
    vi.mocked(api.patch).mockClear()
    vi.mocked(api.del).mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('no_import_cycle', () => {
    for (const src of [projectsSrc, taskSignalsSrc, housekeepingSrc, sourceHealthSrc, notificationInboxLibSrc]) {
      expect(src).not.toContain('notificationInbox')
    }
    expect(notificationInboxLibSrc).not.toMatch(/from 'vue'|from 'pinia'|\.\/api/)
  })

  it('candidates_scope_to_active_workspace_and_skip_quiet', () => {
    const { projects, inbox } = setup()
    setChats(projects, [
      makeChat('mine', { last_read_at: '' }),
      makeChat('other-ws', { project_id: 'p-work', last_read_at: '' }),
      makeChat('archived', { archived: true, last_read_at: '' }),
      makeChat('helper', { helper: { kind: 'memory_pass', state: 'running' }, last_read_at: '' }),
      makeChat('quiet'),
    ])
    expect(inbox.chatCandidates.map((c) => c.chatId)).toEqual(['mine'])
  })

  it('needs_you_and_unread_one_row', () => {
    const { projects, signals, inbox } = setup()
    setChats(projects, [makeChat('c1', { last_read_at: '', pending_permission: '{"tool":"Bash"}' })])
    markHealthy(projects, signals)
    expect(inbox.rows).toHaveLength(1)
    expect(inbox.rows[0].section).toBe('needsYou')
    expect(inbox.rows[0].unread).toBe(true)
    expect(inbox.count).toBe(1)
  })

  it('active_read_chat_keeps_permission', () => {
    const { projects, signals, inbox } = setup()
    setChats(projects, [makeChat('c1', { pending_permission: '{"tool":"Bash"}' })])
    projects.activeChatId = 'c1'
    markHealthy(projects, signals)
    expect(inbox.rows.map((r) => r.key)).toEqual(['chat:c1'])
    expect(inbox.rows[0].unread).toBe(false)
  })

  it('empty_not_all_clear_until_ready', () => {
    const { projects, signals, inbox } = setup()
    expect(inbox.overall).toBe('checking')
    expect(inbox.allClear).toBe(false)
    projects.chatsHealth = ready()
    signals.healthWorkspace = 'personal'
    signals.health = ready()
    expect(inbox.allClear).toBe(false)
    useHousekeepingStore().actionsHealth = ready()
    expect(inbox.allClear).toBe(false)
    useTaskStore().schedulesLoaded = true
    expect(inbox.allClear).toBe(true)
  })

  it('failed_source_then_recovered', () => {
    const { projects, signals, inbox } = setup()
    setChats(projects, [makeChat('c1', { last_read_at: '' })])
    markHealthy(projects, signals)
    projects.chatsHealth = unavailable()
    expect(inbox.overall).toBe('unavailable')
    expect(inbox.allClear).toBe(false)
    const unavailableName = inbox.accessibleName
    projects.chatsHealth = stale()
    expect(inbox.overall).toBe('stale')
    expect(inbox.rows[0].freshness).toBe('stale')
    expect(inbox.count).toBe(1)
    projects.chatsHealth = ready()
    expect(inbox.rows[0].freshness).toBe('fresh')
    expect(inbox.accessibleName).toBe('Notifications, 1 item')
    expect(unavailableName).toContain('could not be fully checked')
  })

  it('signals_health_for_other_workspace_is_checking', () => {
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    signals.health = ready()
    signals.healthWorkspace = 'work'
    expect(inbox.sources[0].status).toBe('checking')
    // A to B to A: held B health never describes A.
    signals.healthWorkspace = 'personal'
    expect(inbox.sources[0].status).toBe('ready')
    projects.activeWorkspace = 'work'
    expect(inbox.sources[0].status).toBe('checking')
    projects.activeWorkspace = 'personal'
    expect(inbox.sources[0].status).toBe('ready')
    signals.healthWorkspace = 'work'
    expect(inbox.sources[0].status).toBe('checking')
  })

  it('combine_health_precedence', () => {
    const { projects, signals, inbox } = setup()
    signals.healthWorkspace = 'personal'
    const status = (a: SourceHealth, b: SourceHealth) => {
      projects.chatsHealth = a
      signals.health = b
      return inbox.snapshots[0]
    }
    expect(status(unavailable(), checking()).status).toBe('unavailable')
    expect(status(checking(), stale()).status).toBe('checking')
    expect(status(stale(), ready()).status).toBe('stale')
    expect(status(ready(), ready()).status).toBe('ready')
    expect(status(ready('2026-10-26T10:00:00.000Z'), ready('2026-10-26T09:00:00.000Z')).lastSuccessAt)
      .toBe('2026-10-26T09:00:00.000Z')
    expect(status(ready(), checking()).lastSuccessAt).toBe('')
  })

  it('retry_chats_never_succeeded_calls_fetch_all_else_sync_latest', async () => {
    const { projects, signals, inbox } = setup()
    const fetchAll = vi.spyOn(projects, 'fetchAll').mockRejectedValue(new Error('down'))
    const syncLatest = vi.spyOn(projects, 'syncLatest').mockResolvedValue()
    const reload = vi.spyOn(signals, 'reload').mockResolvedValue()
    await expect(inbox.retry('chats')).resolves.toBeUndefined()
    expect(fetchAll).toHaveBeenCalledTimes(1)
    expect(syncLatest).not.toHaveBeenCalled()
    expect(reload).toHaveBeenCalledWith('personal')
    projects.chatsHealth = ready()
    await inbox.retry()
    expect(fetchAll).toHaveBeenCalledTimes(1)
    expect(syncLatest).toHaveBeenCalledTimes(1)
  })

  it('refresh_coalesces', async () => {
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    const gate = deferred()
    const syncLatest = vi.spyOn(projects, 'syncLatest').mockImplementation(() => gate.promise)
    vi.spyOn(signals, 'reload').mockResolvedValue()
    const a = inbox.refresh()
    const b = inbox.refresh()
    const c = inbox.refresh()
    expect(inbox.refreshing).toBe(true)
    gate.resolve()
    await Promise.all([a, b, c])
    expect(syncLatest.mock.calls.length).toBeLessThanOrEqual(2)
    expect(syncLatest.mock.calls.length).toBeGreaterThanOrEqual(1)
    expect(inbox.refreshing).toBe(false)
  })

  it('workspace_generation_rejects_late_work', async () => {
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    const gate = deferred()
    const syncLatest = vi.spyOn(projects, 'syncLatest').mockImplementation(() => gate.promise)
    vi.spyOn(signals, 'reload').mockResolvedValue()
    inbox.init()
    const first = inbox.refresh()
    inbox.refresh() // asks for an extra round
    const gen = inbox.generation
    projects.activeWorkspace = 'work'
    await nextTick()
    expect(inbox.generation).toBe(gen + 1)
    expect(inbox.refreshing).toBe(false)
    gate.resolve()
    await first
    expect(syncLatest).toHaveBeenCalledTimes(1)
    expect(inbox.refreshing).toBe(false)

    // A to B to A variant: a held read from the first A must not run a round.
    const gate2 = deferred()
    syncLatest.mockImplementation(() => gate2.promise)
    const held = inbox.refresh()
    inbox.refresh()
    projects.activeWorkspace = 'personal'
    projects.activeWorkspace = 'work'
    projects.activeWorkspace = 'personal'
    gate2.resolve()
    await held
    expect(syncLatest).toHaveBeenCalledTimes(2)
    expect(inbox.refreshing).toBe(false)
    inbox.dispose()
  })

  it('focus_and_visibility_refresh', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-10-26T10:00:00Z'))
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    const syncLatest = vi.spyOn(projects, 'syncLatest').mockResolvedValue()
    vi.spyOn(signals, 'reload').mockResolvedValue()
    inbox.init()

    window.dispatchEvent(new Event('focus'))
    await vi.advanceTimersByTimeAsync(0)
    expect(syncLatest).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(FOCUS_REFRESH_MIN_MS - 1000)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(syncLatest).toHaveBeenCalledTimes(1)

    // retry ignores the gap.
    await inbox.retry()
    expect(syncLatest).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(FOCUS_REFRESH_MIN_MS)
    Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(syncLatest).toHaveBeenCalledTimes(2)

    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(syncLatest).toHaveBeenCalledTimes(3)
    inbox.dispose()
  })

  it('repeated_init_does_not_multiply_listeners', () => {
    const { inbox } = setup()
    const win = vi.spyOn(window, 'addEventListener')
    const doc = vi.spyOn(document, 'addEventListener')
    const interval = vi.spyOn(globalThis, 'setInterval')
    inbox.init()
    inbox.init()
    inbox.init()
    expect(win.mock.calls.filter(([t]) => t === 'focus')).toHaveLength(1)
    expect(doc.mock.calls.filter(([t]) => t === 'visibilitychange')).toHaveLength(1)
    expect(interval).not.toHaveBeenCalled()
    inbox.dispose()
  })

  it('dispose_stops_listeners', async () => {
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    const syncLatest = vi.spyOn(projects, 'syncLatest').mockResolvedValue()
    vi.spyOn(signals, 'reload').mockResolvedValue()
    inbox.init()
    inbox.dispose()
    inbox.dispose()
    expect(inbox.initialized).toBe(false)
    window.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(syncLatest).not.toHaveBeenCalled()

    inbox.init()
    expect(inbox.initialized).toBe(true)
    window.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(syncLatest).toHaveBeenCalledTimes(1)
    inbox.dispose()
  })

  it('never_mutates', async () => {
    const { projects, signals, inbox } = setup()
    projects.chatsHealth = ready()
    const markRead = vi.spyOn(projects, 'markRead')
    vi.spyOn(projects, 'syncLatest').mockResolvedValue()
    vi.spyOn(signals, 'reload').mockResolvedValue()
    setChats(projects, [makeChat('c1', { last_read_at: '' })])
    inbox.init()
    await inbox.refresh()
    await inbox.retry()
    window.dispatchEvent(new Event('focus'))
    await nextTick()
    expect(inbox.count).toBe(1)
    expect(markRead).not.toHaveBeenCalled()
    expect(api.post).not.toHaveBeenCalled()
    expect(api.patch).not.toHaveBeenCalled()
    expect(api.del).not.toHaveBeenCalled()
    inbox.dispose()
  })

  it('web_push_permission_has_no_effect', async () => {
    const { projects, signals, inbox } = setup()
    const touched = vi.fn()
    const original = Object.getOwnPropertyDescriptor(globalThis, 'Notification')
    Object.defineProperty(globalThis, 'Notification', {
      configurable: true,
      get() {
        touched()
        throw new Error('Notification must not be read')
      },
    })
    try {
      projects.chatsHealth = ready()
      vi.spyOn(projects, 'syncLatest').mockResolvedValue()
      vi.spyOn(signals, 'reload').mockResolvedValue()
      setChats(projects, [makeChat('c1', { last_read_at: '' })])
      expect(inbox.rows).toHaveLength(1)
      await inbox.refresh()
      expect(touched).not.toHaveBeenCalled()
    } finally {
      if (original) Object.defineProperty(globalThis, 'Notification', original)
      else delete (globalThis as Record<string, unknown>).Notification
    }
  })

  describe('operator and automation sources', () => {
    type Sched = Record<string, unknown>
    const action = (id: string, kind: string, o: Record<string, unknown> = {}) => ({
      id, kind, severity: 1, title: `Title ${id}`, detail: '', glyph: '', workspace: '',
      run_label: '', chat_label: '', chat_prompt: '', view_label: '', view_route: '', blocking: false, ...o,
    })
    const schedule = (id: string, o: Sched = {}) => ({
      schedule_id: id, title: `Sched ${id}`, prompt: 'p', frequency: 'daily', enabled: true,
      workspace: 'personal', missed: false, last_status: 'ok', ...o,
    })
    function seed() {
      const ctx = setup()
      markHealthy(ctx.projects, ctx.signals)
      const hk = useHousekeepingStore()
      const tasks = useTaskStore()
      const setActions = (a: unknown[]) => { hk.actions = a as unknown as typeof hk.actions }
      const setSchedules = (a: unknown[]) => { tasks.schedules = a as unknown as typeof tasks.schedules }
      return { ...ctx, hk, tasks, setActions, setSchedules }
    }

    it('operator_inclusion_and_exclusion_matrix', () => {
      const { inbox, setActions } = seed()
      setActions([
        action('pu', 'package-update'),
        action('pv', 'provider-update'),
        action('wu', 'workspace-unmigrated', { blocking: true }),
        action('st', 'skill-triage-pending'),
        action('gs', 'github-star'),
        action('missed-schedules', 'missed-schedules'),
      ])
      expect(inbox.rows.map((r) => `${r.section}:${r.key}`).sort()).toEqual([
        'needsYou:operator:shared:missed-schedules',
        'needsYou:operator:shared:wu',
        'toReview:operator:shared:pu',
        'toReview:operator:shared:pv',
        'toReview:operator:shared:st',
      ])
    })

    it('operator_scope', () => {
      const { projects, inbox, setActions } = seed()
      setActions([
        action('w', 'skill-triage-pending', { workspace: 'work' }),
        action('s', 'skill-triage-pending'),
      ])
      setChats(projects, [makeChat('other-ws', { project_id: 'p-work', last_read_at: '' })])
      expect(inbox.rows.map((r) => r.key)).toEqual(['operator:shared:s'])
      projects.activeWorkspace = 'work'
      expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:other-ws', 'operator:shared:s', 'operator:work:w'])
      projects.activeWorkspace = 'personal'
      expect(inbox.rows.map((r) => r.key)).toEqual(['operator:shared:s'])
    })

    it('package_status_does_not_double_count', () => {
      const { projects, inbox, setActions } = seed()
      projects.packageStatus = { update_available: true } as unknown as typeof projects.packageStatus
      expect(inbox.rows).toHaveLength(0)
      setActions([action('pu', 'package-update', { view_route: '/settings' })])
      expect(inbox.rows.map((r) => r.key)).toEqual(['operator:shared:pu'])
    })

    it('disabled_healthy_schedule_absent', () => {
      const { inbox, setSchedules } = seed()
      setSchedules([
        schedule('h', { enabled: false }),
        schedule('m', { enabled: false, last_status: 'missing-chat' }),
      ])
      expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:m'])
      expect(inbox.rows[0].reason).toBe('Its chat was deleted, so it is turned off.')
    })

    it('recovered_schedule_clears', () => {
      const { inbox, setSchedules } = seed()
      setSchedules([schedule('e', { last_status: 'error' })])
      expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:e'])
      setSchedules([schedule('e', { last_status: 'ok' })])
      expect(inbox.rows).toHaveLength(0)
    })

    it('schedule_in_other_workspace_excluded', () => {
      const { projects, inbox, setSchedules } = seed()
      setChats(projects, [makeChat('wc', { project_id: 'p-work' }), makeChat('pc')])
      setSchedules([
        schedule('other', { workspace: 'work', last_status: 'error' }),
        schedule('viaChat', { workspace: undefined, web_chat_id: 'pc', last_status: 'error' }),
        schedule('viaWorkChat', { workspace: undefined, web_chat_id: 'wc', last_status: 'error' }),
      ])
      expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:viaChat'])
    })

    it('one_time_aggregate_dedupe', () => {
      const { inbox, setActions, setSchedules } = seed()
      setActions([action('missed-schedules', 'missed-schedules')])
      setSchedules([schedule('o', { frequency: 'once', missed: true, last_status: '' })])
      expect(inbox.rows.map((r) => r.key)).toEqual(['operator:shared:missed-schedules'])
    })

    it('matching_failed_chat_suppressed_but_live_question_retained', () => {
      const { projects, inbox, setSchedules } = seed()
      setSchedules([schedule('S', { last_status: 'error' })])
      setChats(projects, [makeChat('c1', { schedule_id: 'S', last_status: 'error', last_read_at: '' })])
      vi.spyOn(projects, 'chatFailed').mockReturnValue(true)
      expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:S'])
      setChats(projects, [makeChat('c1', { schedule_id: 'S', last_read_at: '', pending_permission: '{"tool":"Bash"}' })])
      expect(inbox.rows.map((r) => r.key).sort()).toEqual(['chat:c1', 'schedule:S'])
    })

    it('operator_source_status', () => {
      const { hk, inbox, projects, signals } = seed()
      hk.actionsHealth = checking()
      const status = () => inbox.sources.find((s) => s.source === 'operator')!.status
      expect(status()).toBe('checking')
      hk.actionsHealth = unavailable()
      expect(status()).toBe('unavailable')
      expect(inbox.count).toBe(0)
      expect(inbox.allClear).toBe(false)
      hk.actionsHealth = stale()
      expect(status()).toBe('stale')
      hk.actionsHealth = ready()
      expect(status()).toBe('ready')
      expect(projects.activeWorkspace && signals.health).toBeTruthy()
    })

    it('automations_source_status', () => {
      const { tasks, inbox, setSchedules } = seed()
      const status = () => inbox.sources.find((s) => s.source === 'automations')!.status
      tasks.schedulesLoaded = false
      expect(status()).toBe('checking')
      tasks.scheduleLoadError = 'boom'
      expect(status()).toBe('unavailable')
      expect(inbox.allClear).toBe(false)
      tasks.scheduleLoadError = ''
      tasks.schedulesLoaded = true
      setSchedules([schedule('e', { last_status: 'error' })])
      tasks.scheduleLoadError = 'boom'
      expect(status()).toBe('stale')
      expect(inbox.rows.map((r) => r.key)).toEqual(['schedule:e'])
      expect(inbox.rows[0].freshness).toBe('stale')
    })

    it('automations_stamp_advances_only_while_initialized', () => {
      const { tasks, inbox, setSchedules } = seed()
      const stamp = () => inbox.snapshots.find((s) => s.source === 'automations')!.lastSuccessAt
      setSchedules([])
      expect(stamp()).toBe('')
      inbox.init()
      setSchedules([schedule('a')])
      expect(stamp()).not.toBe('')
      const first = stamp()
      tasks.schedules.push(schedule('b') as unknown as (typeof tasks.schedules)[number])
      expect(stamp()).toBe(first)
      inbox.dispose()
    })

    it('retry_operator_and_automations_use_owner_reads_only', async () => {
      const { hk, tasks, inbox } = seed()
      const hkRefresh = vi.spyOn(hk, 'refresh').mockResolvedValue()
      const fetchSchedules = vi.spyOn(tasks, 'fetchSchedules').mockRejectedValue(new Error('down'))
      const run = vi.spyOn(hk, 'run')
      const dismiss = vi.spyOn(hk, 'dismiss')
      const updateTasks = vi.spyOn(hk, 'refreshUpdateTasks')
      await inbox.retry('operator')
      expect(hkRefresh).toHaveBeenCalledTimes(1)
      expect(fetchSchedules).not.toHaveBeenCalled()
      await expect(inbox.retry('automations')).resolves.toBeUndefined()
      expect(fetchSchedules).toHaveBeenCalledTimes(1)
      expect(hkRefresh).toHaveBeenCalledTimes(1)
      expect(run).not.toHaveBeenCalled()
      expect(dismiss).not.toHaveBeenCalled()
      expect(updateTasks).not.toHaveBeenCalled()
      expect(api.post).not.toHaveBeenCalled()
      expect(api.patch).not.toHaveBeenCalled()
      expect(api.del).not.toHaveBeenCalled()
    })

    it('web_push_permission_denied_no_effect', () => {
      const { inbox, setActions, setSchedules } = seed()
      const original = Object.getOwnPropertyDescriptor(globalThis, 'Notification')
      Object.defineProperty(globalThis, 'Notification', {
        configurable: true,
        get() { throw new Error('Notification must not be read') },
      })
      try {
        setActions([action('a', 'skill-triage-pending')])
        setSchedules([schedule('e', { last_status: 'error' })])
        expect(inbox.rows.map((r) => r.key).sort()).toEqual(['operator:shared:a', 'schedule:e'])
      } finally {
        if (original) Object.defineProperty(globalThis, 'Notification', original)
        else delete (globalThis as Record<string, unknown>).Notification
      }
    })

    it('refresh_coalesces_for_new_sources', async () => {
      const { tasks, inbox } = seed()
      const gate = deferred()
      const fetchSchedules = vi.spyOn(tasks, 'fetchSchedules').mockImplementation(() => gate.promise)
      const a = inbox.refresh(['automations'])
      const b = inbox.refresh(['automations'])
      const c = inbox.refresh(['automations'])
      gate.resolve()
      await Promise.all([a, b, c])
      expect(fetchSchedules.mock.calls.length).toBeGreaterThanOrEqual(1)
      expect(fetchSchedules.mock.calls.length).toBeLessThanOrEqual(2)
    })
  })
})
