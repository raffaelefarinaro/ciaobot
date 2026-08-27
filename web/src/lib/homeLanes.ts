import type { ChatInfo } from './types'

export type HomeTierKey = 'needsYou' | 'working' | 'unread' | 'quiet' | 'older'

export interface HomeTiers {
  needsYou: ChatInfo[]
  working: ChatInfo[]
  unread: ChatInfo[]
  quiet: ChatInfo[]
  older: ChatInfo[]
}

export function chatActivityTimestamp(chat: ChatInfo): string {
  return chat.last_activity_at || chat.created_at
}

export function ageBucket(iso: string, now: Date = new Date()): 'fresh' | 'week' | 'older' {
  const timestamp = Date.parse(iso)
  if (!Number.isFinite(timestamp)) return 'fresh'

  const elapsed = Math.max(0, now.getTime() - timestamp)
  if (elapsed < 24 * 60 * 60 * 1000) return 'fresh'
  if (elapsed < 7 * 24 * 60 * 60 * 1000) return 'week'
  return 'older'
}

/**
 * Assign each active chat to exactly one home tier. The callbacks are kept
 * outside the helper so the store remains the source of truth for state.
 *
 * `needsYou` outranks everything: it is the only tier where the agent is
 * actually blocked on you. Below that, `unread` outranks `working`: an
 * unread chat has a finished answer sitting there to read, which needs
 * exactly as much of your attention right now as one that is still running
 * needs none - the lane header's "N still working" summary already covers
 * in-flight visibility, so a merely-running chat does not need to outrank
 * one with something to read. `unread` sits ahead of `quiet` for the same
 * reason it was pulled out of `quiet` in the first place: calling a chat
 * with an unread badge "quiet" was simply untrue.
 */
export function groupHomeTiers(
  chats: ChatInfo[],
  isNeedsYou: (chatId: string) => boolean,
  isWorking: (chatId: string) => boolean,
  isUnread: (chatId: string) => boolean,
  now: Date = new Date(),
): HomeTiers {
  const tiers: HomeTiers = { needsYou: [], working: [], unread: [], quiet: [], older: [] }
  const ordered = chats.slice().sort((a, b) =>
    chatActivityTimestamp(b).localeCompare(chatActivityTimestamp(a)),
  )

  for (const chat of ordered) {
    if (isNeedsYou(chat.chat_id)) {
      tiers.needsYou.push(chat)
    } else if (isUnread(chat.chat_id)) {
      // Ahead of both working and the age check: an unread chat is worth
      // surfacing even while another chat is actively running, and even if
      // its own last activity is old, which is exactly the case a
      // stale-but-unread chat hits.
      tiers.unread.push(chat)
    } else if (isWorking(chat.chat_id)) {
      tiers.working.push(chat)
    } else if (ageBucket(chatActivityTimestamp(chat), now) === 'older') {
      tiers.older.push(chat)
    } else {
      tiers.quiet.push(chat)
    }
  }

  return tiers
}
