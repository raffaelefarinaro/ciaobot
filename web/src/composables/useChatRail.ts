import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { groupHomeTiers, orderedHomeChats } from '../lib/homeLanes'
import { chatTierMark, railItemLabel, sidebarProjectOrder, type RailItem } from '../lib/titleRail'
import { markRailArrival, resetRailState, takeRailArrival, useTitleRail, type TitleRail } from './useTitleRail'

const KEY = 'chat'
// The second chat of a split keeps its own travel order and arrival, so the two rails never share one.
const BESIDE_KEY = 'chat-beside'

export const markChatRailArrival = (direction: -1 | 1 | 0): void => markRailArrival(KEY, direction)
export const takeChatRailArrival = (beside = false): -1 | 1 | 0 => takeRailArrival(beside ? BESIDE_KEY : KEY)
export const resetChatRailSnapshot = (): void => resetRailState(KEY)

/** A chat's title rail: the current workspace's chats in sidebar order. The open chat's rail
 *  switches the route; the second pane of a split passes `openBeside` to change only itself. */
export function useChatRail(currentChatId: () => string, openBeside?: (chatId: string) => void): TitleRail {
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
    return sidebarProjectOrder(store.workspaceProjects).flatMap(p => store.projectChats(p.project_id)
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

  return useTitleRail(openBeside ? BESIDE_KEY : KEY, {
    scope: () => store.activeWorkspace,
    currentId: currentChatId,
    entries,
    navigate: async id => openBeside ? openBeside(id) : store.switchChat(id),
  })
}
