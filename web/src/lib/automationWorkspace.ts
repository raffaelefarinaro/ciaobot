import type { ChatInfo, ProjectInfo, Schedule, WorkspaceName } from './types'

/**
 * The workspace a schedule belongs to.
 *
 * The stored `workspace` field is authoritative, but an interval entry
 * imported from a loop may carry none: loops derived their workspace from the
 * chat they were bound to. Fall back to that derivation so a migrated entry
 * still lands in the right sidebar group instead of disappearing from all of
 * them.
 */
export function workspaceForSchedule(
  schedule: Schedule,
  chats: ChatInfo[],
  projects: ProjectInfo[],
): WorkspaceName | undefined {
  if (schedule.workspace) return schedule.workspace
  if (!schedule.web_chat_id) return undefined
  const chat = chats.find(item => item.chat_id === schedule.web_chat_id)
  const project = chat
    ? projects.find(item => item.project_id === chat.project_id)
    : undefined
  return project?.workspace
}

export function scheduleInWorkspace(
  schedule: Schedule,
  workspace: WorkspaceName,
  chats: ChatInfo[] = [],
  projects: ProjectInfo[] = [],
): boolean {
  return workspaceForSchedule(schedule, chats, projects) === workspace
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
