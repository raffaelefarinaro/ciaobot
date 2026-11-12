import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { sidebarProjectOrder, type RailItem, type RailMark } from '../lib/titleRail'
import { markRailArrival, takeRailArrival, useTitleRail, type TitleRail } from './useTitleRail'

const KEY = 'project'

export const takeProjectRailArrival = (): -1 | 1 | 0 => takeRailArrival(KEY)
export const markProjectRailArrival = (direction: -1 | 1 | 0): void => markRailArrival(KEY, direction)

/** "N chats", or "no chats": what the rail says under a project's name. */
export function projectRailSubtitle(chatCount: number): string {
  if (chatCount === 0) return 'no chats'
  return chatCount === 1 ? '1 chat' : `${chatCount} chats`
}

/** "<name>, <N chats>" plus ", needs you" | ", working" | ", unread". */
export function projectRailLabel(name: string, subtitle: string, mark: RailMark | null): string {
  const parts = [name, subtitle]
  if (mark === 'needs') parts.push('needs you')
  else if (mark === 'working') parts.push('working')
  else if (mark === 'unread') parts.push('unread')
  return parts.join(', ')
}

/**
 * The open project's title rail: the workspace's projects in sidebar order (the
 * pinned and auto-managed projects first, then the user's drag order). A
 * project's mark is the loudest of its open chats': needs you, then working,
 * then unread.
 */
export function useProjectRail(currentId: () => string): TitleRail {
  const store = useProjectStore()
  const router = useRouter()

  const entries = computed<RailItem[]>(() => sidebarProjectOrder(store.workspaceProjects).map(p => {
    const chats = store.projectChats(p.project_id)
    const mark: RailMark | null = store.projectNeedsInput(p.project_id) > 0
      ? 'needs'
      : chats.some(c => store.chatIsWorking(c.chat_id))
        ? 'working'
        : store.projectUnread(p.project_id) > 0 ? 'unread' : null
    const subtitle = projectRailSubtitle(chats.length)
    return {
      id: p.project_id,
      title: p.name,
      subtitle,
      mark,
      titlePending: false,
      label: projectRailLabel(p.name, subtitle, mark),
    }
  }))

  return useTitleRail(KEY, {
    scope: () => store.activeWorkspace,
    currentId,
    entries,
    // A push a guard cancels or redirects resolves with a failure instead of
    // rejecting; rethrow it so the rail drops its travel order and arrival.
    navigate: async id => {
      const failure = await router.push(`/project/${id}`)
      if (failure) throw failure
    },
  })
}
