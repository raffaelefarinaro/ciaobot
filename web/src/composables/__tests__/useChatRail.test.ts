// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../../stores/projects'
import { resetChatRailSnapshot, useChatRail } from '../useChatRail'

// Recency: a is oldest, c is newest, all in one project, so the sidebar's
// order (and the rail's) is newest first: c, b, a.
const now = Date.now()
function at(secondsAgo: number): string {
  return new Date(now - secondsAgo * 1000).toISOString()
}

function seed() {
  const store = useProjectStore()
  store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
  store.chats = [
    { chat_id: 'a', project_id: 'project-1', title: 'a', archived: false, local: true, last_activity_at: at(300), created_at: at(300) },
    { chat_id: 'b', project_id: 'project-1', title: 'b', archived: false, local: true, last_activity_at: at(200), created_at: at(200) },
    { chat_id: 'c', project_id: 'project-1', title: 'c', archived: false, local: true, last_activity_at: at(100), created_at: at(100) },
  ] as unknown as typeof store.chats
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.activeWorkspace = 'personal'
  return store
}

// New activity in chat `id`, which moves it to the top of its project in the
// sidebar, and so of the live order.
function makeNeedsYou(store: ReturnType<typeof useProjectStore>, id: string) {
  const chat = store.chats.find(c => c.chat_id === id)!
  chat.last_activity_at = at(1)
}

function makeNeedsYouByQuestion(store: ReturnType<typeof useProjectStore>, id: string) {
  const chat = store.chats.find(c => c.chat_id === id)!
  chat.pending_question = JSON.stringify({ questions: [{ question: 'Which option?' }] })
}

function ids(rail: ReturnType<typeof useChatRail>): string[] {
  return rail.items.value.map(i => i.id)
}

describe('useChatRail', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetChatRailSnapshot()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    resetChatRailSnapshot()
  })

  it('follows the sidebar top to bottom: pinned projects, then the user\'s order, chats newest first', () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p-late', name: 'Late', workspace: 'personal', order: 2, is_auto: false },
      { project_id: 'p-early', name: 'Early', workspace: 'personal', order: 1, is_auto: false },
      { project_id: 'general', name: 'General', workspace: 'personal', order: 9, is_auto: true },
    ] as unknown as typeof store.projects
    store.chats = [
      { chat_id: 'late-1', project_id: 'p-late', title: 'l', archived: false, local: true, last_activity_at: at(10), created_at: at(10) },
      { chat_id: 'early-old', project_id: 'p-early', title: 'eo', archived: false, local: true, last_activity_at: at(500), created_at: at(500) },
      { chat_id: 'early-new', project_id: 'p-early', title: 'en', archived: false, local: true, last_activity_at: at(50), created_at: at(50) },
      { chat_id: 'gen-1', project_id: 'general', title: 'g', archived: false, local: true, last_activity_at: at(900), created_at: at(900) },
    ] as unknown as typeof store.chats
    store.projectStreaming = {}
    store.backgroundAgents = {}
    store.activeWorkspace = 'personal'
    // A chat that needs you keeps its sidebar place; only its mark changes.
    makeNeedsYouByQuestion(store, 'late-1')

    const rail = useChatRail(() => 'gen-1')
    expect(ids(rail)).toEqual(['gen-1', 'early-new', 'early-old', 'late-1'])
    expect(rail.items.value.find(i => i.id === 'late-1')!.mark).toBe('needs')
  })

  it('keeps the order fixed after a chat moves in the sidebar', () => {
    const store = seed()
    const rail = useChatRail(() => 'b')
    const before = ids(rail)
    expect(before).toEqual(['c', 'b', 'a'])

    makeNeedsYou(store, 'a')

    expect(ids(rail)).toEqual(before)
  })

  it('a rail select makes the next instance reuse the order', async () => {
    const store = seed()
    const switchChat = vi.spyOn(store, 'switchChat').mockResolvedValue()
    const rail = useChatRail(() => 'b')
    const before = ids(rail)

    makeNeedsYou(store, 'a')
    await rail.select('c')
    expect(switchChat).toHaveBeenCalledWith('c')

    // The panel for chat c mounts after the switch: it must show the order
    // the user was travelling, not the live one (a now sits first).
    const next = useChatRail(() => 'c')
    expect(ids(next)).toEqual(before)
  })

  it('an instance for another chat starts fresh', async () => {
    const store = seed()
    vi.spyOn(store, 'switchChat').mockResolvedValue()
    const rail = useChatRail(() => 'b')
    await rail.select('c')

    makeNeedsYou(store, 'a')
    const other = useChatRail(() => 'a')

    expect(ids(other)).toEqual(['a', 'c', 'b'])
  })

  it('an instance for another workspace starts fresh', async () => {
    const store = seed()
    vi.spyOn(store, 'switchChat').mockResolvedValue()
    const rail = useChatRail(() => 'b')
    await rail.select('c')

    makeNeedsYou(store, 'a')
    store.activeWorkspace = 'work'
    store.projects[0].workspace = 'work'
    const elsewhere = useChatRail(() => 'c')

    expect(ids(elsewhere)).toEqual(['a', 'c', 'b'])
  })

  it('a second setup for the same chat gets a fresh order', async () => {
    const store = seed()
    vi.spyOn(store, 'switchChat').mockResolvedValue()
    const rail = useChatRail(() => 'b')
    await rail.select('c')
    // The first panel for chat c consumes the travel order.
    useChatRail(() => 'c')

    makeNeedsYou(store, 'a')
    const later = useChatRail(() => 'c')

    expect(ids(later)).toEqual(['a', 'c', 'b'])
  })

  it('clears the travel order when switchChat throws', async () => {
    const store = seed()
    vi.spyOn(store, 'switchChat').mockRejectedValue(new Error('guard refused'))
    const rail = useChatRail(() => 'b')

    await expect(rail.select('c')).rejects.toThrow('guard refused')

    makeNeedsYou(store, 'a')
    const retry = useChatRail(() => 'c')
    expect(ids(retry)).toEqual(['a', 'c', 'b'])
  })

  // The store surfaces a refused switch itself; a step must not add an unhandled rejection on top.
  it('step does not leave an unhandled rejection when the switch throws', async () => {
    const store = seed()
    vi.spyOn(store, 'switchChat').mockRejectedValue(new Error('guard refused'))
    const rail = useChatRail(() => 'b')
    const unhandled = vi.fn()
    process.on('unhandledRejection', unhandled)
    try {
      expect(rail.step(1)).toBe(true)
      await new Promise(resolve => setTimeout(resolve, 0))
      await new Promise(resolve => setTimeout(resolve, 0))
      expect(unhandled).not.toHaveBeenCalled()
    } finally {
      process.off('unhandledRejection', unhandled)
    }
  })

  it('ignores a second swipe while a switch is still in flight', async () => {
    const store = seed()
    let finish: () => void = () => {}
    const switchChat = vi.spyOn(store, 'switchChat').mockImplementation(
      () => new Promise<void>(resolve => { finish = resolve }),
    )
    const rail = useChatRail(() => 'b')

    const first = rail.select('c')
    const second = rail.select('a')
    expect(switchChat).toHaveBeenCalledTimes(1)

    finish()
    await Promise.all([first, second])

    // Once the switch settles, a new select goes through again.
    switchChat.mockResolvedValue()
    await rail.select('a')
    expect(switchChat).toHaveBeenCalledTimes(2)
  })
})
