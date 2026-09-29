import { describe, expect, it } from 'vitest'
import {
  isMemoryPassChat,
  isMemoryProject,
  memoryPassChatId,
  memoryPassNeedsAttention,
  memoryPassSource,
  memoryPassState,
  MEMORY_PASS_KIND,
} from '../memoryPass'
import type { ChatInfo, ChatPostprocess, ProjectInfo } from '../types'

function pass(state: 'queued' | 'running' | 'done' | 'attention'): ChatInfo {
  return {
    helper: {
      kind: MEMORY_PASS_KIND,
      source_chat_id: 'source-1',
      archive_path: 'chats/source-1.md',
      doc_path: 'projects/general.md',
      source_title: 'Source',
      source_project: 'General',
      state,
      archive_policy: 'when_clean',
    },
  } as unknown as ChatInfo
}

describe('memoryPass', () => {
  it('recognises a pass by its helper kind, not by its project name', () => {
    expect(isMemoryPassChat(pass('queued'))).toBe(true)
    // A user project may legitimately be called "Memory"; only the kind counts.
    expect(isMemoryPassChat({ helper: { kind: 'proposal', intent: 'review', proposal_ids: [], archive_policy: 'manual' } } as unknown as ChatInfo)).toBe(false)
    expect(isMemoryPassChat({} as ChatInfo)).toBe(false)
    expect(isMemoryPassChat(undefined)).toBe(false)
  })

  it('needs attention only for the unclean state', () => {
    expect(memoryPassNeedsAttention(pass('attention'))).toBe(true)
    // Queued and running are in flight, done auto-archives, and an ordinary
    // chat is none of the above: none of them is the owner's problem.
    expect(memoryPassNeedsAttention(pass('queued'))).toBe(false)
    expect(memoryPassNeedsAttention(pass('running'))).toBe(false)
    expect(memoryPassNeedsAttention(pass('done'))).toBe(false)
    expect(memoryPassNeedsAttention(undefined)).toBe(false)
  })

  it('reads the source conversation off a pass and nothing off anything else', () => {
    // The title travels with it: a pass can outlive the chat it was spawned
    // for, and the row that names the work needs a name either way.
    expect(memoryPassSource(pass('done'))).toEqual({ chatId: 'source-1', title: 'Source' })
    expect(memoryPassSource({} as ChatInfo)).toBeNull()
    expect(memoryPassSource(undefined)).toBeNull()
  })

  it('reads the lifecycle state the queue and the archive settle on', () => {
    expect(memoryPassState(pass('queued'))).toBe('queued')
    expect(memoryPassState(pass('running'))).toBe('running')
    expect(memoryPassState(pass('done'))).toBe('done')
    expect(memoryPassState(pass('attention'))).toBe('attention')
    // Not a pass: no state to read, rather than a default that would read as
    // "queued" and put an ordinary chat into the queue.
    expect(memoryPassState({} as ChatInfo)).toBe('')
    expect(memoryPassState(undefined)).toBe('')
  })

  it('reads the pass chat off the source postprocess record', () => {
    const pp: ChatPostprocess = {
      steps: { memory_pass: { status: 'ok', extra: { chat_id: 'pass-1' } } },
    }
    expect(memoryPassChatId(pp)).toBe('pass-1')
  })

  it('reports no pass for anything that is not that record', () => {
    // `extra` is untyped server data, so a hand-edited value must not reach
    // switchChat as a non-string.
    expect(memoryPassChatId(undefined)).toBe('')
    expect(memoryPassChatId(null)).toBe('')
    expect(memoryPassChatId({ steps: {} })).toBe('')
    expect(memoryPassChatId({
      steps: { insights: { status: 'ok', extra: { wrote: true } } },
    })).toBe('')
    expect(memoryPassChatId({
      steps: { memory_pass: { status: 'ok', extra: { chat_id: 7 } } },
    })).toBe('')
  })

  it('identifies the app-owned memory project by kind', () => {
    expect(isMemoryProject({ kind: 'memory' } as ProjectInfo)).toBe(true)
    expect(isMemoryProject({ name: 'Memory' } as unknown as ProjectInfo)).toBe(false)
    expect(isMemoryProject({} as ProjectInfo)).toBe(false)
    expect(isMemoryProject(undefined)).toBe(false)
  })
})
