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
// - No timer, no initial fetch (the owners load at bootstrap) and no
//   `housekeeping.init`.
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
import { isMemoryPassChat } from '../lib/memoryPass'
import {
  notificationAccessibleName,
  notificationBadgeText,
  projectNotificationInbox,
} from '../lib/notificationInbox'
import type {
  ChatCandidate,
  NotificationSourceKey,
  SourceSnapshot,
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

  const generation = ref(0)
  const initialized = ref(false)
  const inflightKeys = ref<RefreshSourceKey[]>([])
  const refreshing = computed(() => inflightKeys.value.length > 0)

  const inflight = new Map<RefreshSourceKey, Promise<void>>()
  const again = new Set<RefreshSourceKey>()
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
      })
    }
    return out
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
  }

  const REFRESHERS: Record<RefreshSourceKey, (workspace: string) => Promise<unknown>> = {
    // Never succeeded means the whole bootstrap failed, so re-run fetchAll
    // (projects and workspaces are needed for membership). fetchAll rejections
    // are swallowed by allSettled.
    chats: (ws) =>
      Promise.allSettled([
        projects.chatsHealth.lastSuccessAt ? projects.syncLatest() : projects.fetchAll(),
        taskSignals.reload(ws),
      ]),
  }

  const snapshots = computed<SourceSnapshot[]>(() =>
    (Object.keys(SNAPSHOT_BUILDERS) as RefreshSourceKey[]).map((k) => SNAPSHOT_BUILDERS[k]()),
  )

  const inbox = computed(() =>
    projectNotificationInbox({ chats: chatCandidates.value, sources: snapshots.value }),
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
      watch(() => projects.activeWorkspace, resetInflight, { flush: 'sync' })
    })
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
