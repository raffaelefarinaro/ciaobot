// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useArchiveChat } from '../useArchiveChat'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { ChatInfo, Task } from '../../lib/types'

const askConfirm = vi.hoisted(() => vi.fn())
const askArchiveTask = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))
vi.mock('../../lib/archiveTaskChoice', () => ({ askArchiveTask }))

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: 'chat-1',
    attempt_id: 'att-1',
    linked_chats: [],
    created_at: '',
    updated_at: '',
    revision: 'a'.repeat(64),
    relative_path: 'Tasks/ship.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: 'att-1',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

const HOLDER = {
  chat_id: 'chat-1',
  helper: { kind: 'task_delegation', task_id: 'ship', task_revision: '', attempt_id: 'att-1' },
} as unknown as ChatInfo

describe('useArchiveChat', () => {
  let archiveChat: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    askConfirm.mockReset()
    askArchiveTask.mockReset()
    archiveChat = vi.fn().mockResolvedValue(undefined)
    useProjectStore().archiveChat = archiveChat as never
    const signals = useTaskSignalsStore()
    signals.tasks = [task()]
    signals.loadedWorkspace = 'personal'
  })

  it('a chat with no task gets the plain confirm and is archived', async () => {
    askConfirm.mockResolvedValue(true)
    const chat = { chat_id: 'other' } as ChatInfo
    await expect(useArchiveChat().archiveChatWithTaskCheck(chat)).resolves.toBe(true)
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(askArchiveTask).not.toHaveBeenCalled()
    expect(archiveChat).toHaveBeenCalledWith('other')
  })

  it('a cancelled plain confirm archives nothing', async () => {
    askConfirm.mockResolvedValue(false)
    await expect(useArchiveChat().archiveChatWithTaskCheck({ chat_id: 'other' } as ChatInfo)).resolves.toBe(false)
    expect(archiveChat).not.toHaveBeenCalled()
  })

  it('a chat holding an open task asks the task question; keep archives', async () => {
    askArchiveTask.mockResolvedValue('keep')
    await expect(useArchiveChat().archiveChatWithTaskCheck(HOLDER)).resolves.toBe(true)
    expect(askArchiveTask).toHaveBeenCalledWith(expect.objectContaining({ id: 'ship' }), 'personal', false)
    expect(askConfirm).not.toHaveBeenCalled()
    expect(archiveChat).toHaveBeenCalledWith('chat-1')
  })

  it('marked-done archives the chat', async () => {
    askArchiveTask.mockResolvedValue('marked-done')
    await expect(useArchiveChat().archiveChatWithTaskCheck(HOLDER)).resolves.toBe(true)
    expect(archiveChat).toHaveBeenCalledWith('chat-1')
  })

  it('approved does not archive from the client', async () => {
    useTaskSignalsStore().tasks = [task({ status: 'in_review', attempt_state: 'ready_for_review' })]
    askArchiveTask.mockResolvedValue('approved')
    await expect(useArchiveChat().archiveChatWithTaskCheck(HOLDER)).resolves.toBe(true)
    expect(askArchiveTask).toHaveBeenCalledWith(expect.anything(), 'personal', true)
    expect(archiveChat).not.toHaveBeenCalled()
  })

  it('cancel archives nothing', async () => {
    askArchiveTask.mockResolvedValue('cancel')
    await expect(useArchiveChat().archiveChatWithTaskCheck(HOLDER)).resolves.toBe(false)
    expect(archiveChat).not.toHaveBeenCalled()
  })

  it('a done task, or a superseded attempt chat, gets the plain confirm', async () => {
    askConfirm.mockResolvedValue(true)
    useTaskSignalsStore().tasks = [task({ status: 'done' })]
    await useArchiveChat().archiveChatWithTaskCheck(HOLDER)
    useTaskSignalsStore().tasks = [task({ attempt_id: 'att-2', chat_id: 'chat-2' })]
    await useArchiveChat().archiveChatWithTaskCheck(HOLDER)
    expect(askArchiveTask).not.toHaveBeenCalled()
    expect(askConfirm).toHaveBeenCalledTimes(2)
    expect(archiveChat).toHaveBeenCalledTimes(2)
  })

  it('a chat only linked to a task gets the plain archive confirmation', async () => {
    askConfirm.mockResolvedValue(true)
    useTaskSignalsStore().tasks = [task({ chat_id: '', linked_chats: ['linked-only'] })]
    const linked = { chat_id: 'linked-only' } as ChatInfo
    await expect(useArchiveChat().archiveChatWithTaskCheck(linked)).resolves.toBe(true)

    // A link is a relation, not a delegation: the chat holds no task, so the
    // task is never completed or detached on the way out.
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(askConfirm).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ title: 'Archive chat' }))
    expect(askArchiveTask).not.toHaveBeenCalled()
    expect(archiveChat).toHaveBeenCalledWith('linked-only')
    const held = useTaskSignalsStore().tasks[0]!
    expect(held.status).toBe('in_progress')
    expect(held.live_attempt_id).toBe('att-1')
  })
})
