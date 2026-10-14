import { describe, expect, it } from 'vitest'
import type { ChatInfo, ProjectInfo, Schedule } from './types'
import { scheduleInWorkspace, scheduleNeedsAttention } from './automationWorkspace'

describe('automation workspace scoping', () => {
  it('keeps schedules only in their assigned workspace', () => {
    const schedule = { workspace: 'personal' } as Schedule

    expect(scheduleInWorkspace(schedule, 'personal')).toBe(true)
    expect(scheduleInWorkspace(schedule, 'work')).toBe(false)
  })

  it('derives a missing workspace from the bound chat project', () => {
    // An interval entry imported from a loop carries no workspace of its own:
    // loops derived it from the chat they were bound to.
    const schedule = { workspace: '', web_chat_id: 'chat-1' } as Schedule
    const chats = [{ chat_id: 'chat-1', project_id: 'project-work' }] as ChatInfo[]
    const projects = [
      { project_id: 'project-work', workspace: 'work' },
    ] as ProjectInfo[]

    expect(scheduleInWorkspace(schedule, 'work', chats, projects)).toBe(true)
    expect(scheduleInWorkspace(schedule, 'personal', chats, projects)).toBe(false)
  })

  it('prefers the stored workspace over the bound chat', () => {
    const schedule = { workspace: 'personal', web_chat_id: 'chat-1' } as Schedule
    const chats = [{ chat_id: 'chat-1', project_id: 'project-work' }] as ChatInfo[]
    const projects = [
      { project_id: 'project-work', workspace: 'work' },
    ] as ProjectInfo[]

    expect(scheduleInWorkspace(schedule, 'personal', chats, projects)).toBe(true)
  })
})

describe('scheduleNeedsAttention', () => {
  const base = { enabled: true, missed: false, last_status: 'ok' } as Schedule

  it('matches the sidebar warning', () => {
    expect(scheduleNeedsAttention({ ...base, enabled: false, last_status: 'missing-chat' })).toBe(true)
    expect(scheduleNeedsAttention({ ...base, missed: true })).toBe(true)
    expect(scheduleNeedsAttention({ ...base, last_status: 'error' })).toBe(true)
    expect(scheduleNeedsAttention({ ...base, enabled: false, last_status: 'error' })).toBe(false)
    expect(scheduleNeedsAttention({ ...base, last_status: 'skipped' })).toBe(false)
    expect(scheduleNeedsAttention(base)).toBe(false)
  })
})
