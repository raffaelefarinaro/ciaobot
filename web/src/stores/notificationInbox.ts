// The notification inbox adapter: turns signals the owner stores already hold
// into the T1 projection's inputs (`ChatCandidate[]`, `SourceSnapshot[]`) and
// exposes the projected rows, count and source statuses.
//
// It owns no data. Rows are pure computeds over `projects` and `taskSignals`,
// which scope themselves by workspace. What the adapter owns is lifecycle:
// `init()` is called by the shared shell (ChatLayout) so the badge is correct on
// a direct load of `/chat/<id>`, and `refresh()`/`retry()` re-read a source.
//
// Deliberately not done:
// - No events-socket observer. `eventsSocket` is not exported, and the events
//   `snapshot` message already reloads `taskSignals` (its health reflects it).
//   The 15s `projects.syncLatest` poll covers the chat list.
// - No timer, no `housekeeping.init`, and no initial fetch except the update-task
//   listing: the other owners load at bootstrap, but nothing reads that one
//   until Home mounts.
// - No mutation: only `syncLatest`/`fetchAll`/`taskSignals.reload` and read
//   getters are called; never `markRead` or any write request.
//
// Workspace safety: a refresh captures `generation` and drops its bookkeeping
// when the generation moved on (workspace switch or dispose), so late work can
// never touch state for another workspace.

import { defineStore } from 'pinia'
import { computed, effectScope, onScopeDispose, ref, watch } from 'vue'
import type { EffectScope } from 'vue'
import { useProjectStore } from './projects'
import { useTaskSignalsStore } from './taskSignals'
import { useHousekeepingStore } from './housekeeping'
import { useTaskStore } from './tasks'
import { scheduleInWorkspace, scheduleNeedsAttention } from '../lib/automationWorkspace'
import { isMemoryPassChat } from '../lib/memoryPass'
import {
  notificationAccessibleName,
  notificationBadgeText,
  operatorInScope,
  projectNotificationInbox,
} from '../lib/notificationInbox'
import type {
  ChatCandidate,
  MemoryInsightCandidate,
  NotificationSourceKey,
  OperatorCandidate,
  ScheduleCandidate,
  SourceSnapshot,
  TaskReviewCandidate,
  UpdateTaskCandidate,
} from '../lib/notificationInbox'
import { checkingHealth } from '../lib/sourceHealth'
import type { SourceHealth } from '../lib/sourceHealth'

export type RefreshSourceKey = NotificationSourceKey
/** Minimum gap between two focus/visibility driven refreshes. */
export const FOCUS_REFRESH_MIN_MS = 5_000

const HEALTH_RANK: Record<SourceHealth['status'], number> = {
  unavailable: 3,
  checking: 2,
  stale: 1,
  ready: 0,
}

/** Status precedence unavailable > checking > stale > ready; '' poisons lastSuccessAt, else the earlier wins. */
function combineHealth(parts: readonly SourceHealth[]): SourceHealth {
  let worst = parts[0]
  for (const p of parts) if (HEALTH_RANK[p.status] > HEALTH_RANK[worst.status]) worst = p
  let lastSuccessAt = parts[0].lastSuccessAt
  for (const p of parts) {
    if (p.lastSuccessAt === '' || lastSuccessAt === '') lastSuccessAt = ''
    else if (p.lastSuccessAt < lastSuccessAt) lastSuccessAt = p.lastSuccessAt
  }
  return {
    status: worst.status,
    lastSuccessAt,
    error: parts.find((p) => p.error)?.error ?? '',
  }
}

export const useNotificationInboxStore = defineStore('notificationInbox', () => {
  const projects = useProjectStore()
  const taskSignals = useTaskSignalsStore()
  const housekeeping = useHousekeepingStore()
  const tasks = useTaskStore()

  const generation = ref(0)
  const initialized = ref(false)
  const inflightKeys = ref<RefreshSourceKey[]>([])
  const refreshing = computed(() => inflightKeys.value.length > 0)

  const inflight = new Map<RefreshSourceKey, Promise<void>>()
  const again = new Set<RefreshSourceKey>()
  // The tasks store keeps no timestamp, so the adapter stamps list replacements.
  // The stamp only advances while initialized (the watcher lives in init()'s scope).
  const schedulesStampedAt = ref('')
  let lastFocusRefreshAt = 0
  let scope: EffectScope | null = null
  let removeListeners: (() => void) | null = null

  const chatCandidates = computed<ChatCandidate[]>(() => {
    const workspace = projects.activeWorkspace
    const inWorkspace = new Set(
      projects.projects.filter((p) => p.workspace === workspace).map((p) => p.project_id),
    )
    const out: ChatCandidate[] = []
    for (const chat of projects.activeChatsAll) {
      if (!inWorkspace.has(chat.project_id)) continue
      const id = chat.chat_id
      const needsInput = projects.chatNeedsInput(id)
      const failed = projects.chatFailed(id)
      const needsYou = projects.chatNeedsYou(id)
      const unread = projects.chatUnread(id) > 0
      // Perf only: the projection would emit no row for a chat with no flag.
      if (!needsInput && !failed && !needsYou && !unread) continue
      // Only an unread-only chat can be the duplicate result of a review task.
      let reviewTaskId = ''
      if (unread && !needsYou) {
        const task = taskSignals.taskForChat(chat)
        if (task && taskSignals.chatHoldsTask(chat, task) && taskSignals.isAwaitingReview(task)) {
          reviewTaskId = task.id
        }
      }
      out.push({
        chatId: id,
        title: chat.title,
        archived: !!chat.archived,
        quietHelper: isMemoryPassChat(chat),
        needsYou,
        needsInput,
        failed,
        unread,
        activityAt: chat.last_activity_at ?? '',
        pendingQuestion: projects.chatPendingQuestion(id) ?? '',
        snippet: projects.chatLastSnippet(id) ?? '',
        scheduleId: chat.schedule_id ?? '',
        reviewTaskId,
      })
    }
    return out
  })

  const operatorCandidates = computed<OperatorCandidate[]>(() =>
    housekeeping.actions
      .filter((a) => operatorInScope(a.workspace, projects.activeWorkspace))
      .map((a) => ({
        id: a.id,
        kind: a.kind,
        workspace: a.workspace,
        title: a.title,
        detail: a.detail,
        blocking: !!a.blocking,
        viewRoute: a.view_route,
      })),
  )

  const scheduleCandidates = computed<ScheduleCandidate[]>(() =>
    tasks.schedules
      .filter((s) => scheduleInWorkspace(s, projects.activeWorkspace, projects.chats, projects.projects))
      .map((s) => ({
        scheduleId: s.schedule_id,
        title: s.title ?? '',
        prompt: s.prompt ?? '',
        frequency: s.frequency,
        enabled: !!s.enabled,
        missed: !!s.missed,
        lastStatus: s.last_status ?? '',
        needsAttention: scheduleNeedsAttention(s),
      })),
  )

  // inReview is [] unless the loaded workspace is the active one.
  const reviewTaskCandidates = computed<TaskReviewCandidate[]>(() =>
    taskSignals.inReview(projects.activeWorkspace).map((t) => ({ taskId: t.id, title: t.title })),
  )

  const memoryInsightCandidates = computed<MemoryInsightCandidate[]>(() => {
    const workspace = projects.activeWorkspace
    const out: MemoryInsightCandidate[] = []
    for (const r of projects.memoryInsightRows) {
      if (!r.blocking || !r.passChatId) continue
      if (projects.projectFor(r.passChatId)?.workspace !== workspace) continue
      out.push({
        sourceChatId: r.sourceChatId,
        passChatId: r.passChatId,
        title: r.title,
        phase: r.phase as MemoryInsightCandidate['phase'],
        question: r.question,
        timestamp: r.timestamp,
      })
    }
    return out
  })

  // Another workspace's rows never leak: they count only for the workspace they were loaded for.
  const updateTaskCandidates = computed<UpdateTaskCandidate[]>(() => {
    const ws = projects.activeWorkspace
    if (!ws || housekeeping.updateTasksWorkspace !== ws) return []
    return housekeeping.updateTasks.map((t) => {
      // A remembered chat id is never a link: only a live, local, unarchived chat is.
      const chat = t.chat_live && t.chat_id
        ? projects.chats.find((c) => c.chat_id === t.chat_id && !c.archived)
        : undefined
      const id = chat ? chat.chat_id : ''
      return {
        id: t.id,
        revision: t.revision,
        workspace: ws,
        title: t.title,
        status: t.status,
        applicability: t.applicability,
        chatId: id,
        chatLive: !!chat,
        chatNeedsInput: chat ? projects.chatNeedsInput(id) : false,
        chatFailed: chat ? projects.chatFailed(id) : false,
      }
    })
  })

  // One entry per source; a missing key is a compile error once INBOX_SOURCES grows.
  const SNAPSHOT_BUILDERS: Record<RefreshSourceKey, () => SourceSnapshot> = {
    chats: () => {
      // A held health from another workspace must never describe this one.
      const signals =
        taskSignals.healthWorkspace === projects.activeWorkspace
          ? taskSignals.health
          : checkingHealth()
      const h = combineHealth([projects.chatsHealth, signals])
      return { source: 'chats', status: h.status, lastSuccessAt: h.lastSuccessAt }
    },
    // Installation-wide. ProjectSidebar owns the housekeeping poll; before it
    // first answers, the health reads checking.
    operator: () => {
      const h = housekeeping.actionsHealth
      return { source: 'operator', status: h.status, lastSuccessAt: h.lastSuccessAt }
    },
    automations: () => {
      let status: SourceSnapshot['status']
      if (!tasks.schedulesLoaded && !tasks.scheduleLoadError) status = 'checking'
      else if (tasks.scheduleLoadError) status = tasks.schedulesLoaded ? 'stale' : 'unavailable'
      else status = 'ready'
      return { source: 'automations', status, lastSuccessAt: schedulesStampedAt.value }
    },
    updateTasks: () => {
      const ws = projects.activeWorkspace
      if (!ws || housekeeping.updateTasksWorkspace !== ws) {
        return { source: 'updateTasks', status: 'checking', lastSuccessAt: '' }
      }
      const h = housekeeping.updateTasksHealth
      // A listing that worked but could not answer is not a healthy empty list.
      const status = h.status === 'ready' && housekeeping.updateTasksCoverageBlocked ? 'unavailable' : h.status
      return { source: 'updateTasks', status, lastSuccessAt: h.lastSuccessAt }
    },
  }

  const REFRESHERS: Record<RefreshSourceKey, (workspace: string) => unknown> = {
    // Never succeeded means the whole bootstrap failed, so re-run fetchAll
    // (projects and workspaces are needed for membership). fetchAll rejections
    // are swallowed by allSettled.
    chats: (ws) =>
      Promise.allSettled([
        projects.chatsHealth.lastSuccessAt ? projects.syncLatest() : projects.fetchAll(),
        taskSignals.reload(ws),
      ]),
    // Both owners record their own failure; neither throws or needs an interval here.
    operator: () => housekeeping.refresh(),
    automations: () => tasks.fetchSchedules().catch(() => {}),
    // Healthy means no request: housekeeping's own focus and 60s poll keep it fresh.
    updateTasks: (ws) =>
      ((housekeeping.updateTasksHealth.status === 'ready'
        && !housekeeping.updateTasksCoverageBlocked
        && housekeeping.updateTasksWorkspace === ws)
        ? undefined
        : housekeeping.refreshUpdateTasks(ws)),
  }

  const snapshots = computed<SourceSnapshot[]>(() =>
    (Object.keys(SNAPSHOT_BUILDERS) as RefreshSourceKey[]).map((k) => SNAPSHOT_BUILDERS[k]()),
  )

  const inbox = computed(() =>
    projectNotificationInbox({
      workspace: projects.activeWorkspace,
      chats: chatCandidates.value,
      operators: operatorCandidates.value,
      schedules: scheduleCandidates.value,
      reviewTasks: reviewTaskCandidates.value,
      memoryInsights: memoryInsightCandidates.value,
      updateTasks: updateTaskCandidates.value,
      sources: snapshots.value,
    }),
  )
  const rows = computed(() => inbox.value.rows)
  const sections = computed(() => inbox.value.sections)
  const count = computed(() => inbox.value.count)
  const sources = computed(() => inbox.value.sources)
  const overall = computed(() => inbox.value.overall)
  const allClear = computed(() => inbox.value.allClear)
  const badgeText = computed(() => notificationBadgeText(count.value))
  const accessibleName = computed(() => notificationAccessibleName(count.value, overall.value))

  function refreshSource(key: RefreshSourceKey): Promise<void> {
    const existing = inflight.get(key)
    if (existing) {
      again.add(key)
      return existing
    }
    const gen = generation.value
    const run = (async () => {
      try {
        do {
          again.delete(key)
          await REFRESHERS[key](projects.activeWorkspace)
          if (gen !== generation.value) return
        } while (again.has(key))
      } catch {
        // A source read never rejects; owners record the failure in their health.
      } finally {
        if (gen === generation.value) {
          inflight.delete(key)
          again.delete(key)
          inflightKeys.value = inflightKeys.value.filter((k) => k !== key)
        }
      }
    })()
    inflight.set(key, run)
    inflightKeys.value = [...inflightKeys.value, key]
    return run
  }

  async function refresh(keys?: RefreshSourceKey[]): Promise<void> {
    const targets = keys ?? (Object.keys(REFRESHERS) as RefreshSourceKey[])
    await Promise.all(targets.map(refreshSource))
  }

  function retry(source?: RefreshSourceKey): Promise<void> {
    return refresh(source ? [source] : undefined)
  }

  function resetInflight() {
    generation.value += 1
    inflight.clear()
    again.clear()
    inflightKeys.value = []
  }

  function onFocusOrVisible() {
    if (document.visibilityState === 'hidden') return
    const now = Date.now()
    if (now - lastFocusRefreshAt < FOCUS_REFRESH_MIN_MS) return
    lastFocusRefreshAt = now
    void refresh()
  }

  function init() {
    if (initialized.value) return
    initialized.value = true
    scope = effectScope()
    scope.run(() => {
      // projects.ts already reloads task signals on a switch: no refresh here.
      watch(
        () => projects.activeWorkspace,
        () => {
          resetInflight()
          void refreshSource('updateTasks')
        },
        { flush: 'sync' },
      )
      // Shallow on purpose: an in-place push is not a load.
      watch(() => tasks.schedules, () => { schedulesStampedAt.value = new Date().toISOString() }, { flush: 'sync' })
    })
    // housekeeping.init ran before a workspace existed and only the Home strip reacts to switches.
    if (projects.activeWorkspace && housekeeping.updateTasksWorkspace !== projects.activeWorkspace) {
      void refreshSource('updateTasks')
    }
    window.addEventListener('focus', onFocusOrVisible)
    document.addEventListener('visibilitychange', onFocusOrVisible)
    removeListeners = () => {
      window.removeEventListener('focus', onFocusOrVisible)
      document.removeEventListener('visibilitychange', onFocusOrVisible)
    }
  }

  function dispose() {
    if (!initialized.value) return
    scope?.stop()
    scope = null
    removeListeners?.()
    removeListeners = null
    resetInflight()
    initialized.value = false
  }

  onScopeDispose(dispose)

  return {
    generation,
    initialized,
    refreshing,
    chatCandidates,
    operatorCandidates,
    scheduleCandidates,
    reviewTaskCandidates,
    memoryInsightCandidates,
    updateTaskCandidates,
    snapshots,
    inbox,
    rows,
    sections,
    count,
    sources,
    overall,
    allClear,
    badgeText,
    accessibleName,
    init,
    dispose,
    refresh,
    retry,
  }
})
