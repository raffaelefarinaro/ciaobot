import { describe, expect, it } from 'vitest'
import type { ChatInfo } from '../types'
import { groupHomeTiers } from '../homeLanes'

const now = new Date('2026-08-11T12:00:00Z')

function chat(chatId: string, secondsAgo: number): ChatInfo {
  const timestamp = new Date(now.getTime() - secondsAgo * 1000).toISOString()
  return {
    chat_id: chatId,
    project_id: 'project-1',
    title: chatId,
    model: '',
    provider: 'claude',
    mode: '',
    session_id: '',
    created_at: timestamp,
    last_activity_at: timestamp,
    archived: false,
    local: true,
  }
}

describe('groupHomeTiers', () => {
  it('uses priority order and keeps recency within each tier', () => {
    const chats = [
      chat('quiet', 60),
      chat('older', 8 * 24 * 60 * 60),
      chat('working', 2 * 60),
      chat('needs', 3 * 60),
      chat('needs-and-working', 4 * 60),
    ]

    const tiers = groupHomeTiers(
      chats,
      id => id === 'needs' || id === 'needs-and-working',
      id => id === 'working' || id === 'needs-and-working',
      () => false,
      now,
    )

    expect(tiers.needsYou.map(c => c.chat_id)).toEqual(['needs', 'needs-and-working'])
    expect(tiers.working.map(c => c.chat_id)).toEqual(['working'])
    expect(tiers.unread).toEqual([])
    expect(tiers.quiet.map(c => c.chat_id)).toEqual(['quiet'])
    expect(tiers.older.map(c => c.chat_id)).toEqual(['older'])
  })

  // An unread chat is not quiet. Calling it quiet made the tier heading
  // contradict the unread badge the sidebar showed for the same chat.
  it('puts unread ahead of working and quiet, ahead of the age check', () => {
    const chats = [
      chat('fresh-unread', 60),
      chat('stale-unread', 9 * 24 * 60 * 60),
      chat('plain-quiet', 120),
      chat('busy-and-unread', 30),
    ]

    const tiers = groupHomeTiers(
      chats,
      () => false,
      id => id === 'busy-and-unread',
      id => id !== 'plain-quiet',
      now,
    )

    // Unread now wins over working for a chat that is both: it has a
    // finished answer to read, which outranks "still running" needing
    // nothing from the user right now.
    expect(tiers.unread.map(c => c.chat_id)).toEqual(['busy-and-unread', 'fresh-unread', 'stale-unread'])
    expect(tiers.working).toEqual([])
    expect(tiers.quiet.map(c => c.chat_id)).toEqual(['plain-quiet'])
    expect(tiers.older).toEqual([])
  })
})
