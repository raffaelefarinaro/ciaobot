// @vitest-environment jsdom

import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useProjectStore } from '../projects'
import { isCareChat } from '../../lib/automationWorkspace'

const old = '2026-11-20T10:00:00+00:00'
const fresh = '2026-11-27T10:00:00+00:00'

function seed(careId = 'system-memory-curation@personal') {
  const store = useProjectStore()
  store.workspaces = [{ name: 'personal', vault_root: '', default_provider: 'claude', color: 'pink' }] as unknown as typeof store.workspaces
  store.projects = [{ project_id: 'p', name: 'P', workspace: 'personal' }] as unknown as typeof store.projects
  const base = { project_id: 'p', archived: false, local: true, created_at: old, last_read_at: old, last_activity_at: fresh }
  store.chats = [
    { ...base, chat_id: 'care', title: 'Care run', schedule_id: careId },
    { ...base, chat_id: 'routine', title: 'Routine', schedule_id: 'daily-digest' },
  ] as unknown as typeof store.chats
  store.activeWorkspace = 'personal'
  return store
}

describe('Workspace care chat and the attention counts', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('recognises the bare and the per-workspace care schedule id only', () => {
    expect(isCareChat({ schedule_id: 'system-memory-curation' })).toBe(true)
    expect(isCareChat({ schedule_id: 'system-memory-curation@work' })).toBe(true)
    expect(isCareChat({ schedule_id: 'daily-digest' })).toBe(false)
    expect(isCareChat({})).toBe(false)
  })

  it('does not count an unread care chat on any badge, but still counts a routine', () => {
    const store = seed()
    expect(store.chatUnread('care')).toBe(0)
    expect(store.chatIsAttentionItem(store.chats[0])).toBe(false)
    expect(store.chatUnread('routine')).toBe(1)
    expect(store.projectUnread('p')).toBe(1)
    expect(store.workspaceUnread('personal')).toBe(1)
    expect(store.totalUnread).toBe(1)
  })

  it('keeps a failed care run in needs-you and the attention count', () => {
    const store = seed()
    store.chats = store.chats.map(c => c.chat_id === 'care' ? { ...c, last_error: 'boom' } : c)
    expect(store.chatNeedsYou('care')).toBe(true)
    expect(store.chatIsAttentionItem(store.chats[0])).toBe(true)
  })

  it('keeps a care run with a question in needs-you and the attention count', () => {
    const store = seed()
    store.chats = store.chats.map(c => c.chat_id === 'care'
      ? { ...c, pending_question: JSON.stringify({ questions: [{ question: 'Merge?' }] }) }
      : c)
    expect(store.chatNeedsYou('care')).toBe(true)
    expect(store.chatIsAttentionItem(store.chats[0])).toBe(true)
  })

  it('keeps a care run with a permission card in needs-you and the attention count', () => {
    const store = seed()
    store.pendingPermissions = { care: [{ request_id: 'r1', tool_name: 'Bash' }] } as unknown as typeof store.pendingPermissions
    expect(store.chatNeedsYou('care')).toBe(true)
    expect(store.chatIsAttentionItem(store.chats[0])).toBe(true)
  })
})
