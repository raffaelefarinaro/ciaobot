// The notification inbox projection: plain source snapshots in, stable rows,
// sections, a distinct-item count and a source-health summary out.
//
// Vue-free and store-free: every signal arrives as an argument, so the
// grouping, ordering and wording are testable without Pinia or a mount. The
// lib never calls a predicate and never reads the clock; callers pass booleans
// they computed from the projects store.
//
// Store contract (for the store that feeds this):
// - chat candidates come from `projects.activeChatsAll` filtered to the active
//   workspace's projects, plus the existing predicates named on ChatCandidate;
// - `activityAt` is `chat.last_activity_at`;
// - `quietHelper` is `isMemoryPassChat(chat)`. Memory work reaches the inbox
//   through its own source, never as a chat row.
//
// `needsYou` outranks `unread`: a chat that is blocked on the owner is one row
// in "Needs you", with the unread flag as an indicator on it. It never also
// appears under "New replies", so the count is distinct rows, not signals.

export type NotificationSection = 'needsYou' | 'toReview' | 'newReplies'
export const SECTION_ORDER: readonly NotificationSection[] = ['needsYou', 'toReview', 'newReplies']
export const SECTION_TITLES: Record<NotificationSection, string> = {
  needsYou: 'Needs you',
  toReview: 'To review',
  newReplies: 'New replies',
}

export type SourceStatus = 'checking' | 'ready' | 'stale' | 'unavailable'
/** Sources the inbox requires. Later units append keys here; a key with no snapshot counts as 'checking'. */
export const INBOX_SOURCES = ['chats'] as const
export type NotificationSourceKey = typeof INBOX_SOURCES[number]
export const SOURCE_LABELS: Record<NotificationSourceKey, string> = { chats: 'Chats' }

export interface SourceSnapshot {
  source: NotificationSourceKey
  status: SourceStatus
  lastSuccessAt: string // '' when never succeeded
}

export type NotificationKind = 'chat-needs-you' | 'chat-unread'
export type NotificationDestination = { kind: 'chat'; chatId: string }
/** Within a section, lower rank sorts first. Needs-you: input < blocking < failure. */
export type NotificationUrgency = 'input' | 'blocking' | 'failure' | 'review' | 'reply'

export interface NotificationRow {
  key: string
  kind: NotificationKind
  section: NotificationSection
  urgency: NotificationUrgency
  source: NotificationSourceKey
  title: string
  reason: string
  timestamp?: string
  destination: NotificationDestination
  unread: boolean
  freshness: 'fresh' | 'stale'
}

export interface ChatCandidate {
  chatId: string
  title: string
  archived: boolean
  quietHelper: boolean
  needsYou: boolean
  needsInput: boolean
  failed: boolean
  unread: boolean
  activityAt: string
  pendingQuestion: string
  snippet: string
}

export interface NotificationInboxInput {
  chats: readonly ChatCandidate[]
  sources: readonly SourceSnapshot[]
}

export interface NotificationSectionGroup {
  section: NotificationSection
  title: string
  rows: NotificationRow[]
}
export interface SourceReport extends SourceSnapshot {
  label: string
}

export interface NotificationInbox {
  sections: NotificationSectionGroup[]
  rows: NotificationRow[]
  count: number
  sources: SourceReport[]
  overall: SourceStatus
  allClear: boolean
}

const REASON_MAX = 160

const URGENCY_RANK: Record<NotificationUrgency, number> = {
  input: 0,
  blocking: 1,
  failure: 2,
  review: 3,
  reply: 4,
}

function plainText(s: string, max: number): string {
  const text = s.replace(/\s+/g, ' ').trim()
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

function chatRow(c: ChatCandidate, freshness: 'fresh' | 'stale'): NotificationRow | null {
  if (c.archived || c.quietHelper) return null
  const needsYou = c.needsYou || c.needsInput || c.failed
  if (!needsYou && !c.unread) return null

  let reason: string
  if (needsYou) {
    if (c.needsInput) reason = c.pendingQuestion || 'Waiting for your answer or approval'
    else if (c.failed) reason = 'The last turn failed'
    else reason = 'A delegated task is waiting for you'
  } else {
    reason = c.snippet || 'New reply'
  }

  const row: NotificationRow = {
    key: `chat:${c.chatId}`,
    kind: needsYou ? 'chat-needs-you' : 'chat-unread',
    section: needsYou ? 'needsYou' : 'newReplies',
    urgency: needsYou ? (c.needsInput ? 'input' : 'failure') : 'reply',
    source: 'chats',
    title: plainText(c.title, Number.MAX_SAFE_INTEGER) || 'Untitled chat',
    reason: plainText(reason, REASON_MAX),
    destination: { kind: 'chat', chatId: c.chatId },
    unread: c.unread,
    freshness,
  }
  const ts = c.activityAt.trim()
  if (ts) row.timestamp = ts
  return row
}

function compareRows(section: NotificationSection): (a: NotificationRow, b: NotificationRow) => number {
  if (section === 'newReplies') {
    return (a, b) => {
      const at = a.timestamp ?? ''
      const bt = b.timestamp ?? ''
      if (at !== bt) {
        if (!at) return 1
        if (!bt) return -1
        return at < bt ? 1 : -1
      }
      return cmp(a.key, b.key)
    }
  }
  return (a, b) =>
    URGENCY_RANK[a.urgency] - URGENCY_RANK[b.urgency] || cmp(a.title, b.title) || cmp(a.key, b.key)
}

function overallStatus(reports: readonly SourceReport[]): SourceStatus {
  for (const status of ['unavailable', 'checking', 'stale'] as const) {
    if (reports.some((r) => r.status === status)) return status
  }
  return 'ready'
}

export function projectNotificationInbox(input: NotificationInboxInput): NotificationInbox {
  const sources: SourceReport[] = INBOX_SOURCES.map((source) => {
    const snap = input.sources.find((s) => s.source === source)
    return {
      source,
      status: snap ? snap.status : 'checking',
      lastSuccessAt: snap ? snap.lastSuccessAt : '',
      label: SOURCE_LABELS[source],
    }
  })
  const overall = overallStatus(sources)
  const chatsFreshness = sources.find((s) => s.source === 'chats')!.status === 'ready' ? 'fresh' : 'stale'

  // A duplicate key keeps the row in the earlier section, then the first seen.
  const byKey = new Map<string, NotificationRow>()
  for (const c of input.chats) {
    const row = chatRow(c, chatsFreshness)
    if (!row) continue
    const prev = byKey.get(row.key)
    if (!prev || SECTION_ORDER.indexOf(row.section) < SECTION_ORDER.indexOf(prev.section)) {
      byKey.set(row.key, row)
    }
  }

  const all = [...byKey.values()]
  const sections: NotificationSectionGroup[] = SECTION_ORDER.map((section) => ({
    section,
    title: SECTION_TITLES[section],
    rows: all.filter((r) => r.section === section).sort(compareRows(section)),
  }))
  const rows = sections.flatMap((s) => s.rows)

  return {
    sections,
    rows,
    count: rows.length,
    sources,
    overall,
    allClear: rows.length === 0 && overall === 'ready',
  }
}

export function notificationPath(d: NotificationDestination): string {
  switch (d.kind) {
    case 'chat':
      return `/chat/${encodeURIComponent(d.chatId)}`
    default: {
      const unreachable: never = d.kind
      return unreachable
    }
  }
}

export function notificationBadgeText(count: number): string {
  return count > 99 ? '99+' : String(count)
}

export function notificationAccessibleName(count: number, overall: SourceStatus): string {
  const base =
    count === 0 ? 'Notifications, none' : `Notifications, ${count} ${count === 1 ? 'item' : 'items'}`
  return overall === 'ready' ? base : `${base}. Some sources could not be fully checked`
}
