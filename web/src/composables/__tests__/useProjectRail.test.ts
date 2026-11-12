// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../../stores/projects'
import { resetRailState } from '../useTitleRail'

const push = vi.fn(async (_to: string) => {})
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))

const { projectRailLabel, projectRailSubtitle, takeProjectRailArrival, useProjectRail } = await import('../useProjectRail')

function project(id: string, order: number, overrides: Record<string, unknown> = {}) {
  return { project_id: id, name: `name ${id}`, workspace: 'personal', context: '', order, ...overrides }
}

function chat(id: string, projectId: string) {
  return { chat_id: id, project_id: projectId, title: id, created_at: '2026-01-01T00:00:00Z', last_activity_at: '2026-01-01T00:00:00Z' }
}

describe('useProjectRail', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    store.chats = []
    push.mockClear()
  })
  afterEach(() => resetRailState('project'))

  it('follows the sidebar: pinned and auto projects first, then the drag order; other workspaces and Memory left out', () => {
    const store = useProjectStore()
    store.projects = [
      project('mine-b', 2),
      project('general', 5, { is_auto: true }),
      project('mine-a', 1),
      project('elsewhere', 0, { workspace: 'work' }),
      project('memory', 3, { kind: 'memory' }),
    ] as never
    const rail = useProjectRail(() => 'mine-a')
    expect(rail.items.value.map(i => i.id)).toEqual(['general', 'mine-a', 'mine-b'])
  })

  it('names the open chats under the title and marks the loudest state', () => {
    const store = useProjectStore()
    store.projects = [project('a', 0), project('b', 1)] as never
    store.chats = [chat('c1', 'a'), chat('c2', 'a')] as never
    vi.spyOn(store, 'chatIsWorking').mockImplementation(id => id === 'c1')
    const rail = useProjectRail(() => 'a')
    const [a, b] = rail.items.value
    expect(a.subtitle).toBe('2 chats')
    expect(a.mark).toBe('working')
    expect(a.label).toBe('name a, 2 chats, working')
    expect(b.subtitle).toBe('no chats')
    expect(b.mark).toBeNull()
  })

  it('steps by routing to the neighbour and records the side it came from', async () => {
    useProjectStore().projects = [project('a', 0), project('b', 1)] as never
    const rail = useProjectRail(() => 'a')
    expect(rail.step(1)).toBe(true)
    await Promise.resolve()
    expect(push).toHaveBeenCalledWith('/project/b')
    expect(takeProjectRailArrival()).toBe(1)
    // No wrap-around at the end.
    expect(useProjectRail(() => 'b').step(1)).toBe(false)
  })

  it('is off for a lone project', () => {
    useProjectStore().projects = [project('a', 0)] as never
    expect(useProjectRail(() => 'a').items.value).toEqual([])
  })
})

describe('project rail words', () => {
  it('counts chats and names the state', () => {
    expect(projectRailSubtitle(1)).toBe('1 chat')
    expect(projectRailSubtitle(3)).toBe('3 chats')
    expect(projectRailLabel('Site', '1 chat', 'needs')).toBe('Site, 1 chat, needs you')
    expect(projectRailLabel('Site', 'no chats', null)).toBe('Site, no chats')
  })
})
