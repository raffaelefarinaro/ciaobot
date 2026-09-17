import { describe, expect, it, vi } from 'vitest'
import { startFileDiscussion } from './fileDiscussion'

type StoreArg = Parameters<typeof startFileDiscussion>[0]

/** The slice of the project store the helper actually touches. */
interface FakeProject { project_id: string; name: string; workspace: string; is_auto: boolean }

function makeStore(overrides: Record<string, unknown> = {}) {
  const store = {
    activeWorkspace: 'personal',
    activeChatId: 'chat-before' as string | null,
    chats: [{ chat_id: 'chat-before' }],
    projects: [
      { project_id: 'p-personal', name: 'General', workspace: 'personal', is_auto: true },
      { project_id: 'p-work', name: 'General', workspace: 'work', is_auto: true },
    ] as FakeProject[],
    fileComments: {} as Record<string, unknown[]>,
    // Mirrors the store's own helper, which the real one resolves through.
    generalProject: (ws: string): FakeProject | null =>
      (store.projects as FakeProject[]).find(
        p => p.workspace === ws && p.is_auto && p.name === 'General',
      ) ?? null,
    switchWorkspace: vi.fn(),
    switchChat: vi.fn(),
    createChat: vi.fn(async () => ({ chat_id: 'c1' })),
    pinFile: vi.fn(),
    pushErrorToast: vi.fn(),
    ...overrides,
  }
  // The real `switchWorkspace` clears the active chat; `switchChat` puts both
  // the chat and its workspace back. The fakes have to model that, or a
  // restore that only fixes the workspace would still pass.
  store.switchWorkspace.mockImplementation(async (ws: string) => {
    store.activeWorkspace = ws
    store.activeChatId = null
  })
  store.switchChat.mockImplementation(async (chatId: string) => {
    store.activeWorkspace = 'personal'
    store.activeChatId = chatId
  })
  return store
}

/** The fake stands in for the real store, which needs a mounted app. */
function asStore(store: ReturnType<typeof makeStore>): StoreArg {
  return store as unknown as StoreArg
}

describe('startFileDiscussion', () => {
  it('creates the chat in the active workspace General and pins the file', async () => {
    const store = makeStore()
    const chat = await startFileDiscussion(asStore(store), {
      path: 'memory-vault/People/Mo.md',
      seed: 'hello',
    })

    expect(chat).toEqual({ chat_id: 'c1' })
    expect(store.switchWorkspace).not.toHaveBeenCalled()
    expect(store.createChat).toHaveBeenCalledWith('p-personal', 'Discuss Mo.md', 'hello')
    expect(store.pinFile).toHaveBeenCalledWith('c1', 'memory-vault/People/Mo.md')
  })

  it('switches to the named workspace first, so the note is read against its own vault', async () => {
    const store = makeStore()
    await startFileDiscussion(asStore(store), { path: 'w/n.md', seed: 'hello', workspace: 'work' })

    expect(store.switchWorkspace).toHaveBeenCalledWith('work')
    expect(store.createChat).toHaveBeenCalledWith('p-work', 'Discuss n.md', 'hello')
  })

  it('appends comments left on the file to the opening message', async () => {
    const store = makeStore()
    store.fileComments = { 'a/b.md': [{ comment: 'why this line?', selection: 'the line' }] }
    await startFileDiscussion(asStore(store), { path: 'a/b.md', seed: 'hello' })

    const [, , seed] = store.createChat.mock.calls[0] as unknown as [string, string, string]
    expect(seed.startsWith('hello')).toBe(true)
    expect(seed).toContain('why this line?')
  })

  it('reports a missing General project by name instead of creating a chat', async () => {
    const store = makeStore({ projects: [] })
    const chat = await startFileDiscussion(asStore(store), { path: 'a/b.md', seed: 'hello' })

    expect(chat).toBeNull()
    expect(store.createChat).not.toHaveBeenCalled()
    expect(store.pushErrorToast).toHaveBeenCalledWith(
      'Cannot start chat',
      'No General project found in the personal workspace.',
    )
  })

  it('stays in the current workspace when the target has no General project', async () => {
    const store = makeStore({
      projects: [{ project_id: 'p-personal', name: 'General', workspace: 'personal', is_auto: true }],
    })
    const chat = await startFileDiscussion(asStore(store), {
      path: 'a/b.md',
      seed: 'hello',
      workspace: 'work',
    })

    expect(chat).toBeNull()
    expect(store.switchWorkspace).not.toHaveBeenCalled()
    expect(store.activeWorkspace).toBe('personal')
  })

  it('puts the chat the user was in back when the cross-workspace creation fails', async () => {
    // Reversing the workspace alone is not a restore: the switch out
    // disconnected and cleared the active chat, so the user would be left on
    // the home screen having lost the conversation they were in.
    const store = makeStore({
      createChat: vi.fn(async () => { throw new Error('offline') }),
    })
    const chat = await startFileDiscussion(asStore(store), {
      path: 'w/n.md',
      seed: 'hello',
      workspace: 'work',
    })

    expect(chat).toBeNull()
    expect(store.switchChat).toHaveBeenCalledWith('chat-before')
    expect(store.activeWorkspace).toBe('personal')
    expect(store.activeChatId).toBe('chat-before')
  })

  it('falls back to the workspace when there was no chat to return to', async () => {
    const store = makeStore({
      activeChatId: null,
      chats: [],
      createChat: vi.fn(async () => { throw new Error('offline') }),
    })
    const chat = await startFileDiscussion(asStore(store), {
      path: 'w/n.md',
      seed: 'hello',
      workspace: 'work',
    })

    expect(chat).toBeNull()
    expect(store.switchChat).not.toHaveBeenCalled()
    expect(store.switchWorkspace.mock.calls.map(c => c[0])).toEqual(['work', 'personal'])
    expect(store.activeWorkspace).toBe('personal')
  })

  it('surfaces a failed creation as a toast and pins nothing', async () => {
    const store = makeStore({
      createChat: vi.fn(async () => { throw new Error('offline') }),
    })
    const chat = await startFileDiscussion(asStore(store), { path: 'a/b.md', seed: 'hello' })

    expect(chat).toBeNull()
    expect(store.pinFile).not.toHaveBeenCalled()
    expect(store.pushErrorToast).toHaveBeenCalledWith('Could not start discussion', 'offline')
  })
})
