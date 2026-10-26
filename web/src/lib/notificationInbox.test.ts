import { describe, expect, it } from 'vitest'
import {
  notificationAccessibleName,
  notificationBadgeText,
  notificationPath,
  projectNotificationInbox,
  type ChatCandidate,
  type SourceSnapshot,
} from './notificationInbox'

function chat(o: Partial<ChatCandidate> & { chatId: string }): ChatCandidate {
  return {
    title: '',
    archived: false,
    quietHelper: false,
    needsYou: false,
    needsInput: false,
    failed: false,
    unread: false,
    activityAt: '',
    pendingQuestion: '',
    snippet: '',
    ...o,
  }
}

const READY: SourceSnapshot[] = [{ source: 'chats', status: 'ready', lastSuccessAt: '2026-10-01T00:00:00+00:00' }]

function project(chats: ChatCandidate[], sources: SourceSnapshot[] = READY) {
  return projectNotificationInbox({ chats, sources })
}

function keys(inbox: ReturnType<typeof project>, section: string): string[] {
  return inbox.sections.find((s) => s.section === section)!.rows.map((r) => r.key)
}

describe('notificationInbox', () => {
  it('needs_you_beats_unread', () => {
    const inbox = project([
      chat({ chatId: 'a', title: 'Alpha', needsInput: true, needsYou: true, unread: true, pendingQuestion: 'Which branch?', activityAt: '2026-10-01T10:00:00+00:00' }),
      chat({ chatId: 'b', title: 'Beta', unread: true, snippet: 'Done: 3 files', activityAt: '2026-10-01T11:00:00+00:00' }),
    ])
    expect(keys(inbox, 'needsYou')).toEqual(['chat:a'])
    const a = inbox.sections[0].rows[0]
    expect(a.unread).toBe(true)
    expect(a.reason).toBe('Which branch?')
    expect(a.urgency).toBe('input')
    expect(keys(inbox, 'newReplies')).toEqual(['chat:b'])
    expect(inbox.sections[2].rows[0].reason).toBe('Done: 3 files')
    expect(inbox.count).toBe(2)
  })

  it('active_read_chat_keeps_permission', () => {
    const inbox = project([chat({ chatId: 'p', needsInput: true, needsYou: true, unread: false })])
    expect(inbox.sections[0].rows).toHaveLength(1)
    const row = inbox.sections[0].rows[0]
    expect(row.unread).toBe(false)
    expect(row.reason).toBe('Waiting for your answer or approval')
    expect(inbox.count).toBe(1)
  })

  it('archived_and_quiet_helpers_excluded', () => {
    const inbox = project([
      chat({ chatId: 'arch', archived: true, unread: true, needsYou: true }),
      chat({ chatId: 'quiet', quietHelper: true, unread: true }),
      chat({ chatId: 'plain' }),
      chat({ chatId: 'ok', unread: true }),
    ])
    expect(inbox.rows.map((r) => r.key)).toEqual(['chat:ok'])
    expect(inbox.count).toBe(1)
  })

  it('same_titles_order_by_key', () => {
    const input = [
      chat({ chatId: 'c3', title: 'Same', failed: true }),
      chat({ chatId: 'c1', title: 'Same', failed: true }),
      chat({ chatId: 'c2', title: 'Same', failed: true }),
      chat({ chatId: 'z', title: 'Zed', needsInput: true }),
      chat({ chatId: 'r2', title: 'Same', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
      chat({ chatId: 'r1', title: 'Same', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
    ]
    const before = input.map((c) => c.chatId)
    const inbox = project(input)
    expect(keys(inbox, 'needsYou')).toEqual(['chat:z', 'chat:c1', 'chat:c2', 'chat:c3'])
    expect(keys(inbox, 'newReplies')).toEqual(['chat:r1', 'chat:r2'])
    expect(input.map((c) => c.chatId)).toEqual(before)
  })

  it('badge_counts_rows_not_messages', () => {
    const x = chat({ chatId: 'x', needsYou: true, unread: true })
    const inbox = project([x, x, chat({ chatId: 'y', unread: true })])
    expect(inbox.count).toBe(2)
    expect(notificationBadgeText(99)).toBe('99')
    expect(notificationBadgeText(100)).toBe('99+')
    expect(notificationBadgeText(0)).toBe('0')
    expect(notificationAccessibleName(100, 'ready')).toBe('Notifications, 100 items')
    expect(notificationAccessibleName(1, 'ready')).toBe('Notifications, 1 item')
    expect(notificationAccessibleName(0, 'ready')).toBe('Notifications, none')
  })

  it('zero_with_unavailable_source_is_not_all_clear', () => {
    const unavailable = project([], [{ source: 'chats', status: 'unavailable', lastSuccessAt: '' }])
    expect(unavailable.count).toBe(0)
    expect(unavailable.overall).toBe('unavailable')
    expect(unavailable.allClear).toBe(false)

    const checking = project([], [{ source: 'chats', status: 'checking', lastSuccessAt: '' }])
    expect(checking.overall).toBe('checking')
    expect(checking.allClear).toBe(false)

    const missing = project([], [])
    expect(missing.overall).toBe('checking')
    expect(missing.sources[0]).toMatchObject({ source: 'chats', status: 'checking', lastSuccessAt: '', label: 'Chats' })
    expect(missing.allClear).toBe(false)

    expect(project([]).allClear).toBe(true)

    const stale = project([chat({ chatId: 'u', unread: true })], [{ source: 'chats', status: 'stale', lastSuccessAt: 'x' }])
    expect(stale.rows).toHaveLength(1)
    expect(stale.rows[0].freshness).toBe('stale')
    expect(stale.allClear).toBe(false)

    const name = notificationAccessibleName(0, 'unavailable')
    expect(name).toContain('could not be fully checked')
    expect(name.toLowerCase()).not.toContain('all clear')
  })

  it('unknown_timestamps_are_not_invented', () => {
    const inbox = project([
      chat({ chatId: 'u1', unread: true, activityAt: '' }),
      chat({ chatId: 'u2', unread: true, activityAt: '2026-10-01T09:00:00+00:00' }),
      chat({ chatId: 'u3', unread: true, activityAt: '   ' }),
      chat({ chatId: 'n', needsYou: true }),
    ])
    const by = (k: string) => inbox.rows.find((r) => r.key === k)!
    expect('timestamp' in by('chat:u1')).toBe(false)
    expect('timestamp' in by('chat:u3')).toBe(false)
    expect('timestamp' in by('chat:n')).toBe(false)
    expect(keys(inbox, 'newReplies')).toEqual(['chat:u2', 'chat:u1', 'chat:u3'])
    expect(by('chat:u2').timestamp).toBe('2026-10-01T09:00:00+00:00')
  })

  it('orders replies newest first', () => {
    const inbox = project([
      chat({ chatId: 'a', unread: true, activityAt: '2026-10-01T08:00:00+00:00' }),
      chat({ chatId: 'b', unread: true, activityAt: '2026-10-01T12:00:00+00:00' }),
      chat({ chatId: 'c', unread: true, activityAt: '2026-10-01T10:00:00+00:00' }),
    ])
    expect(keys(inbox, 'newReplies')).toEqual(['chat:b', 'chat:c', 'chat:a'])
  })

  it('builds an encoded chat path', () => {
    expect(notificationPath({ kind: 'chat', chatId: 'a/b c' })).toBe('/chat/a%2Fb%20c')
  })

  it('collapses and truncates reasons', () => {
    const long = 'line one\n\n' + 'x'.repeat(300)
    const inbox = project([chat({ chatId: 'u', unread: true, snippet: long })])
    const reason = inbox.rows[0].reason
    expect(reason).not.toMatch(/\s{2,}|\n/)
    expect(reason.length).toBeLessThanOrEqual(160)
    expect(reason.endsWith('…')).toBe(true)
  })

  it('failed chat without input is a failure', () => {
    const inbox = project([chat({ chatId: 'f', failed: true })])
    expect(inbox.rows[0].urgency).toBe('failure')
    expect(inbox.rows[0].reason).toBe('The last turn failed')
    expect(inbox.rows[0].title).toBe('Untitled chat')
  })
})
