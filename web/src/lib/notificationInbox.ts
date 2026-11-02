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

import { isVisibleUpdateTask } from './updateTasks'

export type NotificationSection = 'needsYou' | 'toReview' | 'newReplies'
export const SECTION_ORDER: readonly NotificationSection[] = ['needsYou', 'toReview', 'newReplies']
export const SECTION_TITLES: Record<NotificationSection, string> = {
  needsYou: 'Needs you',
  toReview: 'To review',
  newReplies: 'New replies',
}

export type SourceStatus = 'checking' | 'ready' | 'stale' | 'unavailable'
/** Sources the inbox requires. Later units append keys here; a key with no snapshot counts as 'checking'. */
export const INBOX_SOURCES = ['chats', 'operator', 'automations', 'updateTasks'] as const
export type NotificationSourceKey = typeof INBOX_SOURCES[number]
export const SOURCE_LABELS: Record<NotificationSourceKey, string> = {
  chats: 'Chats',
  operator: 'Settings',
  automations: 'Automations',
  updateTasks: 'Update tasks',
}

export interface SourceSnapshot {
  source: NotificationSourceKey
  status: SourceStatus
  lastSuccessAt: string // '' when never succeeded
}

export type NotificationKind =
  | 'chat-needs-you'
  | 'chat-unread'
  | 'operator-action'
  | 'missed-schedules'
  | 'automation-attention'
  | 'task-review'
  | 'memory-attention'
  | 'update-task'
export type NotificationDestination =
  | { kind: 'chat'; chatId: string }
  | { kind: 'schedule'; scheduleId: string }
  | { kind: 'settings'; tab: '' | 'providers' }
  | { kind: 'home-notice'; noticeKey: string }
  | { kind: 'task'; taskId: string }
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
  /** The schedule that produced this chat ('' or absent when none). */
  scheduleId?: string
  /**
   * Id of the awaiting-review task whose CURRENT attempt this unread-only chat
   * holds, else '' or absent. Adapter: set only when unread and not needsYou.
   */
  reviewTaskId?: string
}

export interface OperatorCandidate {
  id: string
  kind: string
  /** '' = installation-wide. */
  workspace: string
  title: string
  detail: string
  blocking: boolean
  /** OperatorAction.view_route, '' when none. */
  viewRoute: string
}

export interface ScheduleCandidate {
  scheduleId: string
  title: string
  prompt: string
  frequency: string
  enabled: boolean
  missed: boolean
  lastStatus: string
  /** Adapter: scheduleInWorkspace(...) && scheduleNeedsAttention(s). */
  needsAttention: boolean
}

/** A board task whose agent finished and waits for Approve (`taskSignals.inReview`). */
export interface TaskReviewCandidate {
  taskId: string
  title: string
}

/** A blocking memory-insight row (`projects.memoryInsightRows`, blocking only). */
export interface MemoryInsightCandidate {
  sourceChatId: string
  passChatId: string
  title: string
  phase: 'needsYou' | 'attention'
  question: string
  timestamp: string
}

/** An update task for the workspace on screen (`housekeeping.updateTasks`). */
export interface UpdateTaskCandidate {
  id: string
  revision: number
  workspace: string
  title: string
  status: string
  applicability: string
  /** '' unless chat_live AND the chat exists locally and is not archived. Never a dead remembered chat. */
  chatId: string
  chatLive: boolean
  /** Only ever true when chatId !== ''. */
  chatNeedsInput: boolean
  chatFailed: boolean
}

export interface NotificationInboxInput {
  /** Workspace on screen; scopes row keys. '' keys as 'shared'. */
  workspace?: string
  chats: readonly ChatCandidate[]
  operators?: readonly OperatorCandidate[]
  schedules?: readonly ScheduleCandidate[]
  reviewTasks?: readonly TaskReviewCandidate[]
  memoryInsights?: readonly MemoryInsightCandidate[]
  updateTasks?: readonly UpdateTaskCandidate[]
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

/** Operator kinds that never reach the inbox. */
export const EXCLUDED_OPERATOR_KINDS: readonly string[] = ['github-star']
const MISSED_SCHEDULES_KIND = 'missed-schedules'

/** Mirrors HousekeepingStrip.scopedActions: installation-wide or the active workspace. */
export function operatorInScope(actionWorkspace: string, activeWorkspace: string): boolean {
  return !activeWorkspace || !actionWorkspace || actionWorkspace === activeWorkspace
}

/** Mirrors HomeReviewSummary.automationName. */
export function scheduleDisplayName(s: { title: string; prompt: string; scheduleId: string }): string {
  const title = (s.title || '').trim()
  if (title) return title
  const line = (s.prompt || '').trim().split('\n')[0].trim()
  if (line) return line.length > 60 ? `${line.slice(0, 57)}…` : line
  return s.scheduleId
}

function stripMarkdown(s: string): string {
  return s
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
    .replace(/^\s*(?:- |# )/gm, '')
}

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

/** Key of an operator-action Home notice. The strip and the inbox both build keys here. */
export function actionNoticeKey(workspace: string, id: string): string {
  return `action:${workspace || 'shared'}:${id}`
}

/** Key of an update-task Home notice, scoped to the workspace on screen. */
export function updateNoticeKey(activeWorkspace: string, taskId: string, revision: number): string {
  return `update:${activeWorkspace || 'shared'}:${taskId}@${revision}`
}

const HOME_NOTICE_KEY = /^(action|update):([^:]+):(.+)$/

export function parseHomeNoticeKey(
  key: string,
): { kind: 'action' | 'update'; workspace: string; id: string } | null {
  const m = HOME_NOTICE_KEY.exec(key)
  if (!m) return null
  return { kind: m[1] as 'action' | 'update', workspace: m[2], id: m[3] }
}

function operatorDestination(o: OperatorCandidate): NotificationDestination {
  if (o.kind === 'package-update') return { kind: 'settings', tab: '' }
  if (o.kind === 'provider-update') return { kind: 'settings', tab: 'providers' }
  if (o.viewRoute === '/settings') return { kind: 'settings', tab: '' }
  if (o.viewRoute === '/settings/providers') return { kind: 'settings', tab: 'providers' }
  return { kind: 'home-notice', noticeKey: actionNoticeKey(o.workspace, o.id) }
}

function operatorRow(o: OperatorCandidate, freshness: 'fresh' | 'stale'): NotificationRow | null {
  if (EXCLUDED_OPERATOR_KINDS.includes(o.kind)) return null
  const key = `operator:${o.workspace || 'shared'}:${o.id}`
  if (o.kind === MISSED_SCHEDULES_KIND) {
    return {
      key,
      kind: 'missed-schedules',
      section: 'needsYou',
      urgency: 'failure',
      source: 'operator',
      title: plainText(`All workspaces: ${o.title}`, Number.MAX_SAFE_INTEGER),
      reason: 'All workspaces. Open Home to fire or dismiss them.',
      destination: { kind: 'home-notice', noticeKey: actionNoticeKey(o.workspace, o.id) },
      unread: false,
      freshness,
    }
  }
  return {
    key,
    kind: 'operator-action',
    section: o.blocking ? 'needsYou' : 'toReview',
    urgency: o.blocking ? 'blocking' : 'review',
    source: 'operator',
    title: plainText(o.title, Number.MAX_SAFE_INTEGER),
    reason: plainText(stripMarkdown(o.detail), REASON_MAX),
    destination: operatorDestination(o),
    unread: false,
    freshness,
  }
}

/** The enabled one-time reminder Home already covers with the missed-schedules notice. */
function coveredByMissedAggregate(s: ScheduleCandidate): boolean {
  return s.frequency === 'once' && s.enabled && s.missed
    && s.lastStatus !== 'error' && s.lastStatus !== 'missing-chat'
}

function automationRow(s: ScheduleCandidate, freshness: 'fresh' | 'stale'): NotificationRow {
  const reason =
    s.lastStatus === 'missing-chat' ? 'Its chat was deleted, so it is turned off.'
    : s.lastStatus === 'error' ? 'The last run failed.'
    : 'It did not run when expected.'
  return {
    key: `schedule:${s.scheduleId}`,
    kind: 'automation-attention',
    section: 'needsYou',
    urgency: 'failure',
    source: 'automations',
    title: plainText(scheduleDisplayName(s), Number.MAX_SAFE_INTEGER),
    reason,
    destination: { kind: 'schedule', scheduleId: s.scheduleId },
    unread: false,
    freshness,
  }
}

function taskReviewRow(t: TaskReviewCandidate, ws: string, freshness: 'fresh' | 'stale'): NotificationRow {
  return {
    key: `task:${ws}:${t.taskId}`,
    kind: 'task-review',
    section: 'toReview',
    urgency: 'review',
    source: 'chats',
    title: plainText(t.title, Number.MAX_SAFE_INTEGER),
    reason: 'The agent says it is done. Approve or send it back.',
    destination: { kind: 'task', taskId: t.taskId },
    unread: false,
    freshness,
  }
}

function memoryInsightRow(m: MemoryInsightCandidate, freshness: 'fresh' | 'stale'): NotificationRow {
  const needsYou = m.phase === 'needsYou'
  const row: NotificationRow = {
    key: `memory:${m.sourceChatId}`,
    kind: 'memory-attention',
    section: 'needsYou',
    urgency: needsYou ? 'input' : 'failure',
    source: 'chats',
    title: plainText(`Memory update: ${m.title}`, Number.MAX_SAFE_INTEGER),
    reason: plainText(
      needsYou
        ? m.question || 'The memory update is waiting for your answer or approval'
        : 'The memory update needs attention',
      REASON_MAX,
    ),
    destination: { kind: 'chat', chatId: m.passChatId },
    unread: false,
    freshness,
  }
  const ts = m.timestamp.trim()
  if (ts) row.timestamp = ts
  return row
}

function updateTaskRow(t: UpdateTaskCandidate, freshness: 'fresh' | 'stale'): NotificationRow | null {
  if (!isVisibleUpdateTask(t)) return null
  // A live question or permission: the chat row owns it.
  if (t.chatNeedsInput) return null
  // Ordinary work is not a notification.
  if (t.status === 'in_progress' && !t.chatFailed) return null

  const failure = t.status === 'failed' || (t.status === 'in_progress' && t.chatFailed)
  const offeredUnknown = t.status === 'offered' && t.applicability === 'unknown'
  let reason: string
  if (t.status === 'failed') reason = 'The last attempt did not get going.'
  else if (t.status === 'in_progress') reason = 'Its chat failed.'
  else if (t.status === 'waiting_review') {
    reason = t.chatLive
      ? 'Its chat is open and waiting for you to decide what to do with it.'
      : 'Its chat was archived or deleted. Open the notice to start a new one.'
  } else if (offeredUnknown) reason = 'Not yet known whether this applies here. Open it to check again.'
  else reason = 'New since this update.'

  return {
    key: `update-task:${t.workspace || 'shared'}:${t.id}@${t.revision}`,
    kind: 'update-task',
    section: failure ? 'needsYou' : 'toReview',
    urgency: failure ? 'failure' : 'review',
    source: 'updateTasks',
    title: plainText(offeredUnknown ? `Check applicability: ${t.title}` : t.title, Number.MAX_SAFE_INTEGER),
    reason,
    destination: { kind: 'home-notice', noticeKey: updateNoticeKey(t.workspace, t.id, t.revision) },
    unread: false,
    freshness,
  }
}

/** The failed chat of an incident, and its unread-result twin. A live question or a delegated wait is not one. */
function isIncidentTwin(c: ChatCandidate): boolean {
  return !c.needsInput && (c.failed || !c.needsYou)
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
  const freshnessOf = (source: NotificationSourceKey): 'fresh' | 'stale' =>
    sources.find((s) => s.source === source)!.status === 'ready' ? 'fresh' : 'stale'

  // A duplicate key keeps the row in the earlier section, then the first seen.
  const byKey = new Map<string, NotificationRow>()
  const add = (row: NotificationRow | null) => {
    if (!row) return
    const prev = byKey.get(row.key)
    if (!prev || SECTION_ORDER.indexOf(row.section) < SECTION_ORDER.indexOf(prev.section)) {
      byKey.set(row.key, row)
    }
  }

  const operators = input.operators ?? []
  for (const o of operators) add(operatorRow(o, freshnessOf('operator')))

  const hasMissedAggregate = operators.some((o) => o.kind === MISSED_SCHEDULES_KIND)
  const incidentSchedules = new Set<string>()
  for (const s of input.schedules ?? []) {
    if (!s.needsAttention) continue
    if (hasMissedAggregate && coveredByMissedAggregate(s)) continue
    add(automationRow(s, freshnessOf('automations')))
    incidentSchedules.add(s.scheduleId)
  }

  const ws = input.workspace || 'shared'
  const reviewedTaskIds = new Set<string>()
  for (const t of input.reviewTasks ?? []) {
    add(taskReviewRow(t, ws, freshnessOf('chats')))
    reviewedTaskIds.add(t.taskId)
  }
  const passChatIds = new Set<string>()
  for (const m of input.memoryInsights ?? []) {
    add(memoryInsightRow(m, freshnessOf('chats')))
    passChatIds.add(m.passChatId)
  }

  const updateTaskChatIds = new Set<string>()
  for (const t of input.updateTasks ?? []) {
    const row = updateTaskRow(t, freshnessOf('updateTasks'))
    if (!row) continue
    add(row)
    if (t.chatId) updateTaskChatIds.add(t.chatId)
  }

  for (const c of input.chats) {
    // The unread result of a delegated task is the same item as its review row.
    // A live question, a failure or a delegated wait stays.
    if (c.reviewTaskId && reviewedTaskIds.has(c.reviewTaskId) && !c.needsYou && !c.needsInput && !c.failed) continue
    if (passChatIds.has(c.chatId)) continue
    // The failed chat of an incident, and its unread-result twin, are the same
    // problem as the schedule row. A live question or a delegated wait stays.
    if (c.scheduleId && incidentSchedules.has(c.scheduleId) && isIncidentTwin(c)) continue
    if (updateTaskChatIds.has(c.chatId) && isIncidentTwin(c)) continue
    add(chatRow(c, freshnessOf('chats')))
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
    case 'schedule':
      return `/schedules/${encodeURIComponent(d.scheduleId)}`
    case 'settings':
      return d.tab ? `/settings/${d.tab}` : '/settings'
    case 'home-notice':
      return `/?notice=${encodeURIComponent(d.noticeKey)}`
    case 'task':
      return `/tasks/${encodeURIComponent(d.taskId)}`
    default: {
      const unreachable: never = d
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
