import type { ChatInfo, Loop, ProjectInfo, Schedule, WorkspaceName } from './types'

export function scheduleInWorkspace(
  schedule: Schedule,
  workspace: WorkspaceName,
): boolean {
  return schedule.workspace === workspace
}

export function workspaceForLoop(
  loop: Loop,
  chats: ChatInfo[],
  projects: ProjectInfo[],
): WorkspaceName | undefined {
  const chat = chats.find(item => item.chat_id === loop.web_chat_id)
  const project = chat
    ? projects.find(item => item.project_id === chat.project_id)
    : undefined
  return project?.workspace
}

export function loopInWorkspace(
  loop: Loop,
  workspace: WorkspaceName,
  chats: ChatInfo[],
  projects: ProjectInfo[],
): boolean {
  return workspaceForLoop(loop, chats, projects) === workspace
}

/**
 * Whether this schedule is one row of a per-workspace fan-out.
 *
 * A packaged routine marked `per_workspace` is installed once per workspace with
 * a `<base-id>@<workspace>` id, so the separator is what distinguishes "this row
 * owns a workspace" from "this row was pointed at one".
 */
export function isPerWorkspaceRoutine(schedule: Schedule): boolean {
  return schedule.scope === 'system' && schedule.schedule_id.includes('@')
}
