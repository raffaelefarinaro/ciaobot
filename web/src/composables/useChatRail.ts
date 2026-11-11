import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { groupHomeTiers, orderedHomeChats } from '../lib/homeLanes'
import { chatTierMark, railItemLabel, type RailItem } from '../lib/titleRail'
import { markRailArrival, resetRailState, takeRailArrival, useTitleRail, type TitleRail } from './useTitleRail'

const KEY = 'chat'

export const markChatRailArrival = (direction: -1 | 1 | 0): void => markRailArrival(KEY, direction)
export const takeChatRailArrival = (): -1 | 1 | 0 => takeRailArrival(KEY)
export const resetChatRailSnapshot = (): void => resetRailState(KEY)

/** The open chat's title rail: the current workspace's chats in sidebar order. */
export function useChatRail(currentChatId: () => string): TitleRail {
  const store = useProjectStore()

  // The sidebar's order, top to bottom: the pinned projects (General and the other
  // auto-managed ones) as the server orders them, then the user's projects in their
  // drag order, each with its chats newest first. Home's tiers only supply each
  // chat's state for the track's marks.
  const entries = computed<RailItem[]>(() => {
    const ws = store.activeWorkspace
    const chats = store.activeChatsAll.filter(c => store.projectFor(c.chat_id)?.workspace === ws)
    const tierOf = new Map(orderedHomeChats(groupHomeTiers(
      chats,
      id => store.chatNeedsYou(id),
      id => store.chatIsWorking(id),
      id => store.chatUnread(id) > 0,
    )).map(e => [e.chat.chat_id, e.tier]))
    const projects = store.workspaceProjects
    const sidebarProjects = [...projects.filter(p => p.is_auto), ...projects.filter(p => !p.is_auto)]
    return sidebarProjects.flatMap(p => store.projectChats(p.project_id)
      .filter(c => tierOf.has(c.chat_id))
      .map(chat => {
        const tier = tierOf.get(chat.chat_id)!
        const project = store.projectFor(chat.chat_id)?.name ?? ''
        const titlePending = chat.title_status === 'pending'
        return {
          id: chat.chat_id,
          title: chat.title,
          subtitle: project,
          mark: chatTierMark(tier),
          titlePending,
          label: railItemLabel({ title: chat.title, project, tier, titlePending }),
        }
      }))
  })

  return useTitleRail(KEY, {
    scope: () => store.activeWorkspace,
    currentId: currentChatId,
    entries,
    navigate: id => store.switchChat(id),
  })
}
