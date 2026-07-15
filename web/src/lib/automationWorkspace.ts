import type { ChatInfo, Loop, ProjectInfo, Schedule, WorkspaceName } from './types'

export function scheduleInWorkspace(
  schedule: Schedule,
  workspace: WorkspaceName,
): boolean {
  return schedule.workspace === workspace
}

export function loopInWorkspace(
  loop: Loop,
  workspace: WorkspaceName,
  chats: ChatInfo[],
  projects: ProjectInfo[],
): boolean {
  const chat = chats.find(item => item.chat_id === loop.web_chat_id)
  const project = chat
    ? projects.find(item => item.project_id === chat.project_id)
    : undefined
  return project?.workspace === workspace
}
