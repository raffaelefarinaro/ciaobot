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
})
