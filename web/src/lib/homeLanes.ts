import type { ChatInfo } from './types'

export type HomeTierKey = 'needsYou' | 'working' | 'quiet' | 'older'

export interface HomeTiers {
  needsYou: ChatInfo[]
  working: ChatInfo[]
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
 */
export function groupHomeTiers(
  chats: ChatInfo[],
  isNeedsYou: (chatId: string) => boolean,
  isWorking: (chatId: string) => boolean,
  now: Date = new Date(),
): HomeTiers {
  const tiers: HomeTiers = { needsYou: [], working: [], quiet: [], older: [] }
  const ordered = chats.slice().sort((a, b) =>
    chatActivityTimestamp(b).localeCompare(chatActivityTimestamp(a)),
  )

  for (const chat of ordered) {
    if (isNeedsYou(chat.chat_id)) {
      tiers.needsYou.push(chat)
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
