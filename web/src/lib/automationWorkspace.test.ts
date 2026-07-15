import { describe, expect, it } from 'vitest'
import type { ChatInfo, Loop, ProjectInfo, Schedule } from './types'
import { loopInWorkspace, scheduleInWorkspace } from './automationWorkspace'

describe('automation workspace scoping', () => {
  it('keeps schedules only in their assigned workspace', () => {
    const schedule = { workspace: 'personal' } as Schedule

    expect(scheduleInWorkspace(schedule, 'personal')).toBe(true)
    expect(scheduleInWorkspace(schedule, 'work')).toBe(false)
  })

  it('derives a loop workspace from its fixed chat project', () => {
    const loop = { web_chat_id: 'chat-1' } as Loop
    const chats = [{ chat_id: 'chat-1', project_id: 'project-work' }] as ChatInfo[]
    const projects = [
      { project_id: 'project-work', workspace: 'work' },
    ] as ProjectInfo[]

    expect(loopInWorkspace(loop, 'work', chats, projects)).toBe(true)
    expect(loopInWorkspace(loop, 'personal', chats, projects)).toBe(false)
  })
})
