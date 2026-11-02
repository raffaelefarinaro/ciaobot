// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../../stores/projects'
import { resetChatRailSnapshot, useChatRail } from '../useChatRail'

// Recency: a is oldest, c is newest. All three are quiet and fresh, so the
// fresh order is newest first: c, b, a.
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

// Makes chat `id` a needs-you chat, which moves it to the top of the live order.
function makeNeedsYou(store: ReturnType<typeof useProjectStore>, id: string) {
  const chat = store.chats.find(c => c.chat_id === id)!
  chat.pending_question = JSON.stringify({ questions: [{ question: 'Which option?' }] })
}

function ids(rail: ReturnType<typeof useChatRail>): string[] {
  return rail.items.value.map(i => i.chatId)
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

  it('keeps the order fixed after a chat changes tier', () => {
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
