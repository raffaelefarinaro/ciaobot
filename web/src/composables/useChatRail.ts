import { computed, type ComputedRef } from 'vue'
import { useProjectStore } from '../stores/projects'
import { groupHomeTiers, orderedHomeChats } from '../lib/homeLanes'
import { railNeighbours, stableRailOrder, type ChatRailItem } from '../lib/chatRail'

// Module-level so it survives ChatPanel remounts (ChatLayout keys ChatPanel by chat id).
let snapshot: { workspace: string; chatId: string; ids: string[] } | null = null
// True while a rail switch is in flight; a second swipe during it is ignored.
let switching = false

export function resetChatRailSnapshot(): void {
  snapshot = null
}

export function useChatRail(currentChatId: () => string): {
  items: ComputedRef<ChatRailItem[]>
  neighbours: ComputedRef<{ index: number; previous: string | null; next: string | null }>
  select: (chatId: string) => Promise<void>
  step: (direction: -1 | 1) => boolean
} {
  const store = useProjectStore()

  const fresh = computed(() => {
    const ws = store.activeWorkspace
    const chats = store.activeChatsAll.filter(c => store.projectFor(c.chat_id)?.workspace === ws)
    return orderedHomeChats(groupHomeTiers(
      chats,
      id => store.chatNeedsYou(id),
      id => store.chatIsWorking(id),
      id => store.chatUnread(id) > 0,
    ))
  })

  // Frozen at setup: ChatPanel remounts per chat, so "setup" = "arrived at this chat".
  // The travel order is consumed here, so only the panel a rail switch mounts reuses it.
  const saved = snapshot
  snapshot = null
  const reuse = saved !== null && saved.workspace === store.activeWorkspace && saved.chatId === currentChatId()
  const frozen: string[] = reuse && saved ? saved.ids : fresh.value.map(e => e.chat.chat_id)

  const items = computed<ChatRailItem[]>(() => {
    const byId = new Map(fresh.value.map(e => [e.chat.chat_id, e]))
    const order = stableRailOrder(fresh.value.map(e => e.chat.chat_id), frozen)
    if (!byId.has(currentChatId()) || order.length < 2) return []
    return order.map(id => {
      const entry = byId.get(id)!
      return {
        chatId: id,
        title: entry.chat.title,
        project: store.projectFor(id)?.name ?? '',
        tier: entry.tier,
        titlePending: entry.chat.title_status === 'pending',
      }
    })
  })

  const neighbours = computed(() => railNeighbours(items.value.map(i => i.chatId), currentChatId()))

  async function select(chatId: string): Promise<void> {
    if (switching || chatId === currentChatId()) return
    switching = true
    try {
      snapshot = { workspace: store.activeWorkspace, chatId, ids: items.value.map(i => i.chatId) }
      await store.switchChat(chatId)
    } catch (err) {
      snapshot = null
      throw err
    } finally {
      switching = false
    }
  }

  function step(direction: -1 | 1): boolean {
    const target = direction < 0 ? neighbours.value.previous : neighbours.value.next
    if (!target) return false
    // select clears its snapshot on failure, and the store surfaces switch errors itself.
    select(target).catch(() => {})
    return true
  }

  return { items, neighbours, select, step }
}
