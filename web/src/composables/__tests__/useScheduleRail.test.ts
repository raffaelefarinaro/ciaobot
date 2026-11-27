// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { resetRailState } from '../useTitleRail'

const push = vi.fn(async (_to: string) => {})
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

const { scheduleRailLabel, takeScheduleRailArrival, useScheduleRail } = await import('../useScheduleRail')

function schedule(id: string, overrides: Record<string, unknown> = {}) {
  return {
    schedule_id: id, title: `title ${id}`, prompt: 'p', daily_time_utc: '09:00', timezone_name: 'UTC',
    frequency: 'daily', enabled: true, scope: 'user', workspace: 'personal', web_project_id: 'project-1',
    next_run: '', last_triggered_on: '', days_of_week: null, interval_minutes: 0, day_of_month: null,
    run_at_date: null, web_chat_id: null, missed: false, model: '', archive_policy: 'manual',
    ...overrides,
  }
}

describe('useScheduleRail', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as never
    store.chats = []
    push.mockClear()
  })
  afterEach(() => resetRailState('schedule'))

  it('follows the pages: system, then the user\'s routines by next run (one-offs among them), then loops; other workspaces left out', () => {
    useTaskStore().schedules = [
      schedule('sys', { scope: 'system', next_run: '2026-12-01T06:00:00Z' }),
      schedule('r1', { next_run: '2026-12-01T09:00:00Z' }),
      schedule('once', { frequency: 'once', run_at_date: '2026-12-01', next_run: '2026-12-01T08:00:00Z' }),
      schedule('elsewhere', { workspace: 'work' }),
      schedule('r2', { frequency: 'interval', interval_minutes: 30, next_run: '2026-12-01T10:00:00Z' }),
      schedule('paused', { enabled: false, next_run: '2026-12-01T07:00:00Z' }),
    ] as never
    const rail = useScheduleRail(() => 'r1')
    expect(rail.items.value.map(i => i.id)).toEqual(['sys', 'once', 'r1', 'r2', 'paused'])
    expect(rail.items.value.map(i => i.subtitle.split(' · ')[0])).toEqual(['System', 'One-off', 'Routine', 'Routine', 'Routine'])
    expect(rail.items.value[3].subtitle).toBe('Routine · 30m')
  })

  it('lists a chat loop as a Loop after the routines, with its cadence', () => {
    useTaskStore().schedules = [
      schedule('r1'),
      schedule('lp', {
        frequency: 'interval', interval_minutes: 5, web_chat_id: 'chat-a',
        owner_chat_id: 'chat-a', expires_at: '2099-01-01T00:00:00Z', max_runs: 10, run_count: 0,
      }),
      schedule('once', { frequency: 'once', run_at_date: '2026-12-01' }),
    ] as never
    const rail = useScheduleRail(() => 'lp')
    expect(rail.items.value.map(i => i.id)).toEqual(['once', 'r1', 'lp'])
    const loop = rail.items.value[2]
    expect(loop.subtitle).toBe('Loop · every 5 min')
    expect(loop.label).toBe('title lp, Loop, every 5 min')
  })

  it('marks a missed run as needing you and a paused automation in its name', () => {
    useTaskStore().schedules = [
      schedule('a', { missed: true }),
      schedule('b', { enabled: false }),
    ] as never
    const rail = useScheduleRail(() => 'a')
    const [a, b] = rail.items.value
    expect(a.mark).toBe('needs')
    expect(a.label).toContain('missed a run')
    expect(b.mark).toBeNull()
    expect(b.subtitle).toBe('Routine · paused')
    expect(b.label).toBe('title b, Routine, paused')
  })

  it('steps by routing to the neighbour and records the side it came from', async () => {
    useTaskStore().schedules = [schedule('a'), schedule('b')] as never
    const rail = useScheduleRail(() => 'b')
    expect(rail.step(-1)).toBe(true)
    await Promise.resolve()
    expect(push).toHaveBeenCalledWith('/schedules/a')
    expect(takeScheduleRailArrival()).toBe(-1)
  })

  it('is off for a lone automation', () => {
    useTaskStore().schedules = [schedule('a')] as never
    expect(useScheduleRail(() => 'a').items.value).toEqual([])
  })
})

describe('scheduleRailLabel', () => {
  it('names the title, group, time and state', () => {
    expect(scheduleRailLabel('Brief', 'Routine', '08:00', null)).toBe('Brief, Routine, 08:00')
    expect(scheduleRailLabel('Brief', 'Routine', '08:00', 'working')).toBe('Brief, Routine, 08:00, working')
    expect(scheduleRailLabel('Brief', 'Routine', 'paused', null)).toBe('Brief, Routine, paused')
    expect(scheduleRailLabel('Brief', 'Routine', '', 'needs')).toBe('Brief, Routine, missed a run')
  })
})

describe('schedule rail subtitles', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as never
    store.chats = []
  })
  afterEach(() => resetRailState('schedule'))

  it('gives a manual routine no time, and a paused one-off the same "paused" in subtitle and label', () => {
    useTaskStore().schedules = [
      schedule('once', { frequency: 'once', run_at_date: '2026-12-01', enabled: false }),
      schedule('manual', { frequency: 'manual' }),
    ] as never
    // The paused one-off sorts after the routine that will run.
    const [manual, once] = useScheduleRail(() => 'manual').items.value
    expect(manual.subtitle).toBe('Routine')
    expect(manual.label).toBe('title manual, Routine')
    expect(once.subtitle).toBe('One-off · paused')
    expect(once.label).toBe('title once, One-off, paused')
  })

  it('rolls the rail back when a guard cancels the navigation', async () => {
    useTaskStore().schedules = [schedule('a'), schedule('b')] as never
    push.mockResolvedValueOnce({ type: 4 } as never)
    const rail = useScheduleRail(() => 'a')
    await expect(rail.select('b')).rejects.toEqual({ type: 4 })
    expect(takeScheduleRailArrival()).toBe(0)
  })
})
