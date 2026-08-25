import { defineStore } from 'pinia'
import { ref, computed, watch, toRaw } from 'vue'
import { api } from '../lib/api'
import { getPendingBucket, normalizePendingBuckets, setPendingBucket } from '../lib/pendingBuckets'
import { buildFixPrompt } from '../lib/fixError'
import { formatChatComments, formatFileComments, type ChatCommentAnchor } from '../lib/commentContext'
import { isPlausibleFilePath } from '../lib/filePaths'
import { useFileViewerStore } from './fileViewer'
import { isRateLimitTelemetry } from '../lib/rateLimit'
import { readReentrySummaryEnabled } from '../composables/useReentrySummaryPreference'
import {
  isRestartDrainMessage,
  reloadWhenServerReady,
  restartMessageForDisplay,
} from '../lib/serverRestart'
import { archiveFailedToast, archiveStoppedToast } from '../lib/archiveCopy'
import { errorMessage } from '../lib/errorMessage'
import { clearChatDraft, readChatDraft, readOrphanCandidates, writeChatDraft } from '../lib/chatDrafts'
import { isPostprocessing, postprocessNeedsInsights } from '../lib/postprocessView'
import type {
  ArchiveChatResponse,
  ProjectInfo,
  ChatInfo,
  ChatPostprocess,
  ChatRow,
  ChatGroup,
  ChatMessage,
  SubagentTranscript,
  WsEvent,
  EventsWsMessage,
  VoiceResult,
  InAppToast,
  PackageStatus,
  PendingPermission,
  RuntimeProvider,
  WorkspaceInfo,
  WorkspaceName,
  WorkspaceProviderOption,
  WorkspacesResponse,
} from '../lib/types'

export function shouldReconnectActiveChatOnStreamingStarted(
  socket: Pick<WebSocket, 'readyState'> | undefined,
): boolean {
  // CONNECTING=0, OPEN=1. Reconnecting in either state replays the broker
  // buffer into a client that may already have consumed live deltas, which
  // duplicates streamed text chunk by chunk.
  return !socket || socket.readyState > 1
}

/** Backoff for unexpected per-chat WS drops. `attempt` is 1-based. */
export function chatWsReconnectDelayMs(attempt: number): number {
  if (attempt <= 1) return 50
  return Math.min(50 * 2 ** (attempt - 1), 2000)
}

/** Compatibility check for host proxies from before `host_unreachable` existed. */
export function isHostConnectionUnavailableMessage(message: string): boolean {
  return message.trim().toLowerCase().startsWith('host ws unreachable')
}

const PROTOTYPE_HAZARD_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

/**
 * Checked element write for comment lists whose index comes from stored data.
 * Skips prototype-hazardous keys and out-of-range indices, and writes through
 * `splice` rather than property assignment, so even a guard bypass could not
 * turn the write into a `__proto__` assignment on the array or its prototype.
 */
export function setListIndex<T>(list: T[], key: number | string, value: T): void {
  if (typeof key === 'string' && PROTOTYPE_HAZARD_KEYS.has(key)) return
  const index = typeof key === 'number' ? key : Number(key)
  if (!Number.isInteger(index) || index < 0 || index >= list.length) return
  list.splice(index, 1, value)
}

// Must match `_DEFAULT_CHAT_TITLE` on the server: `_is_empty_chat` uses it to
// tell an abandoned draft from a chat the user deliberately named.
const DEFAULT_CHAT_TITLE = 'New Chat'

export const useProjectStore = defineStore('projects', () => {
  const projects = ref<ProjectInfo[]>([])
  const chats = ref<ChatInfo[]>([])
  const workspaces = ref<WorkspaceInfo[]>([])
  const workspaceProviderOptions = ref<WorkspaceProviderOption[]>([
    { value: 'claude', label: 'Claude' },
  ])
  // App-wide fallback model when a workspace default_model is empty; lets
  // Settings label the picker "Inherit default (<model>)".
  const workspaceAppDefaultModel = ref('')
  const activeWorkspace = ref<WorkspaceName>('personal')
  const activeChatId = ref<string | null>(null)
  // Re-entry summaries are requested in the background whenever a non-empty
  // chat is opened. They are deliberately ephemeral: the first new message
  // clears the summary so it never becomes part of the conversation history.
  const reentrySummaries = ref<Record<string, string>>({})
  const reentrySummaryRequests = new Set<string>()
  const reentrySummaryRevisions = ref<Record<string, number>>({})
  // False until the first fetchAll() resolves. Gates the home empty state so
  // a restored active chat does not flash a blank placeholder.
  const bootstrapped = ref(false)
  const messages = ref<Record<string, ChatMessage[]>>({})
  // History is loaded independently after a chat becomes active. Keep this
  // separate from `messages` so cached text can render immediately while the
  // authoritative session history is still on the way.
  const loadingMessages = ref<Record<string, boolean>>({})
  const messageLoadGenerations = new Map<string, number>()
  // Pagination state for envelope-mode history loads (see loadMessagesFromServer).
  const historyMeta = ref<Record<string, { total: number; hasMore: boolean; nextOffset: number | null; limit: number } | undefined>>({})
  const loadingOlder = ref<Record<string, boolean>>({})
  const partRequests = new Map<string, Promise<void>>()
  // Subagent transcripts keyed by chat_id. Loaded lazily on chat switch and
  // after each streaming turn (subagents can be spawned mid-turn).
  const subagents = ref<Record<string, SubagentTranscript[]>>({})
  const sockets = ref<Record<string, WebSocket>>({})
  const streaming = ref<Record<string, boolean>>({})
  const streamingText = ref<Record<string, string>>({})
  const streamingTextPhase = ref<Record<string, ChatMessage['phase']>>({})
  // Per-chat in-flight thinking buffer. Mirrors `streamingText` but for
  // `thinking_delta` events: we accumulate the model's reasoning text and
  // commit it as a `kind: 'thinking'` timeline entry the moment a visible
  // text delta or tool_use starts (i.e. thinking has ended). Without this
  // buffer, intermediate thinking blocks emitted by some models would
  // disappear entirely (they used to be silently dropped at end-of-stream).
  const streamingThinking = ref<Record<string, string>>({})
  // Per-chat live token totals for the in-flight turn, fed by `token_usage`
  // WS events. Cleared on turn start and result. Drives the running token
  // count in the "Working..." trace meta.
  const liveUsage = ref<Record<string, { input: number; output: number }>>({})
  // Per-chat epoch millis when the current turn started streaming. Powers the
  // live elapsed timer in the "Working..." trace meta. Cleared on result.
  const streamStartedAt = ref<Record<string, number>>({})
  const pendingImagesByChat = ref<Record<string, string[]>>({})
  const pendingImages = computed<string[]>({
    get: () => getPendingBucket(pendingImagesByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingImagesByChat.value, activeChatId.value, entries)
      persistPendingImages()
    },
  })
  // Pending in-file comments captured from the file viewer. Each entry is
  // a (path, selected text, user note) triple plus an optional source line
  // range (1-indexed, inclusive). Cleared on send (formatted into the
  // outgoing message) or via removePendingComment / clear helpers.
  type PendingComment = {
    id: string
    path: string
    selection: string
    comment: string
    lineStart?: number | null
    lineEnd?: number | null
    colIndex?: number | null
    colHeader?: string | null
    images?: string[]
  }
  const pendingCommentsByChat = ref<Record<string, PendingComment[]>>({})
  const pendingComments = computed<PendingComment[]>({
    get: () => getPendingBucket(pendingCommentsByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingCommentsByChat.value, activeChatId.value, entries)
      persistPendingComments()
    },
  })
  // Durable file comments: persisted per file so they remain visible in the
  // document viewer after being sent. Keyed by workspace-relative path.
  type FileComment = PendingComment & { createdAt: string }
  const fileComments = ref<Record<string, FileComment[]>>({})
  // Chat comments: ephemeral references to text selected inside a chat bubble.
  // Formatted as XML-tagged reference blocks (see lib/commentContext.ts).
  type PendingChatComment = ChatCommentAnchor & {
    id: string
    selection: string
    comment: string
    images?: string[]
  }
  const pendingChatCommentsByChat = ref<Record<string, PendingChatComment[]>>({})
  const pendingChatComments = computed<PendingChatComment[]>({
    get: () => getPendingBucket(pendingChatCommentsByChat.value, activeChatId.value),
    set: (entries) => {
      if (!activeChatId.value) return
      setPendingBucket(pendingChatCommentsByChat.value, activeChatId.value, entries)
      persistPendingChatComments()
    },
  })
  // Pinned file paths per chat/project. Dismissals are remembered per *path*,
  // not per chat: a replayed `file_surface` event (WS reconnect replays the
  // in-flight stream's buffer) must not reopen a file the user closed, but a
  // later surface of a *different* file is a new deliverable and must still
  // open. A chat-wide flag conflated the two and silently swallowed every
  // subsequent surface request for the rest of the chat.
  const pinnedFilePaths = ref<Record<string, string>>({})
  const dismissedAutoPins = ref<Record<string, string[]>>({})
  // 'filecard' carries a file-write tool call (Write/Edit/MultiEdit/NotebookEdit).
  // It breaks contiguous 'tool' groups so the PWA can render a standalone
  // clickable card with a preview link instead of folding it into _activity.
  type StreamEntry =
    | { kind: 'tool'; content: string }
    | { kind: 'thinking'; content: string }
    | { kind: 'text'; content: string; phase?: ChatMessage['phase'] }
    | { kind: 'filecard'; content: string; file_path: string; action: string; tool: string; tool_use_id?: string }
    | { kind: 'status'; content: string }
  const streamingTimeline = ref<Record<string, StreamEntry[]>>({})  // per-chat interleaved tool/text entries
  const unread = ref<Record<string, number>>({})  // per-chat unread assistant message count
  // Per-chat "broker is running for this chat" flag, driven by /ws/events.
  // Distinct from `streaming` (which only fires for the chat whose per-chat
  // WS is open). projectStreaming is what powers sidebar dots on inactive
  // chats and projects.
  const projectStreaming = ref<Record<string, boolean>>({})
  // Per-chat count of background subagents still running *after* the parent
  // turn's result landed. Driven by `chat_subagents_ready` over /ws/events
  // (the server's subagent watcher). Powers a persistent "N background agents
  // running" indicator so the user can see work is ongoing during the quiet
  // gap between the turn ending and the agents reporting back.
  const backgroundAgents = ref<Record<string, number>>({})
  // Full-screen restart overlay while the server drains active chats and
  // relaunches. Driven by /ws/events `server_restarting` (and the same
  // signal on the per-chat socket when a send is rejected mid-drain).
  const serverRestarting = ref(false)
  const serverRestartMessage = ref('')
  // Ephemeral client-mode connection state. Host proxy failures must never
  // enter chat history: reconnect attempts can repeat indefinitely and would
  // otherwise create one error bubble (and one "Fix this error" action) each.
  const hostConnectionUnavailable = ref(false)
  type QueuedMessage = { id: string; text: string; images?: string[] }
  function makeQueuedId(): string {
    if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
      return crypto.randomUUID()
    }
    return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
  }
  // Locally-tracked queued user messages (sent while a response was already
  // streaming). Cleared when the server echoes them back as a user_echo at
  // flush time, or on result when the queue ends up empty.
  const queuedMessages = ref<Record<string, QueuedMessage[]>>({})
  // Pending Auto-mode permission prompts keyed by chat_id. The chat bubble
  // renders Approve/Deny buttons for each entry; clicking sends a
  // `permission_response` on the per-chat WS and pops the entry optimistically.
  const pendingPermissions = ref<Record<string, PendingPermission[]>>({})
  // Per-project "new chat is being created" flag so UI can disable buttons
  // and prevent double-clicks while the POST is in flight.
  const creatingChatProjectIds = ref<Record<string, boolean>>({})
  // Optimistic archiving: chats whose archive POST is in flight. They are
  // removed from active lists immediately and shown in the home "archiving…"
  // queue so the chat panel can close without waiting for the server's disk
  // work (transcript write + delegate cascade). The map is keyed by chat_id
  // and cleared on success (archived stays true, tidying takes over) or on
  // failure (archived is rolled back, row reappears).
  const archivingChats = ref<Record<string, boolean>>({})
  // Chats this client has flipped to `archived: true` locally — optimistically
  // in `archiveChat`, or from a `chat_archived` event — that no `/api/chats`
  // payload has confirmed yet. Every list refresh replaces `chats.value`
  // wholesale, so a GET issued before the archive landed on the server (the 15s
  // poll, or the refresh the `chat_result_ready` / `chat_subagents_ready`
  // handlers fire) resolves with `archived: false` and puts the row back in the
  // sidebar until the next refresh — the archive flicker. `applyPendingArchived`
  // re-applies the local truth to every payload; entries clear as soon as the
  // server agrees (or the chat is gone), and on rollback when the POST fails.
  // A Set, not a Record: the keys are chat ids straight off the websocket, and
  // `obj[id] = true` with `id === "__proto__"` walks up Object.prototype.
  const pendingArchived = ref<Set<string>>(new Set())
  // Not reactive UI state, just an in-flight guard: `creatingChatProjectIds`
  // is a display flag consumers can ignore (a second click landing before
  // Vue re-renders, a duplicated keyboard handler), so a second createChat()
  // call for the same project could still fire before the first POST
  // resolves. The server's create_chat sweeps other empty "New Chat" shells
  // on every call, so two overlapping calls raced each other's chat out of
  // existence right as the panel switched to it. Keying the pending promise
  // by project makes a second call join the first instead of double-posting.
  const pendingChatCreations: Record<string, Promise<ChatInfo>> = {}
  // the tool call with empty answers, so the PWA renders its own picker above
  // the composer. Cleared the next time the user sends a message (their reply
  // implicitly answers, regardless of whether they clicked an option).
  type ActiveQuestionOption = { label: string; description?: string }
  type ActiveQuestion = {
    id: string
    question: string
    header: string
    multiSelect: boolean
    allowOther: boolean
    isSecret: boolean
    requestId: string
    options: ActiveQuestionOption[]
  }
  const activeQuestions = ref<Record<string, ActiveQuestion[]>>({})

  // Signatures of AskUserQuestion pickers the user has already answered or
  // dismissed this session, keyed by chat. Clearing `activeQuestions` on send
  // is only client-side and optimistic; the server clears the persisted
  // `pending_question` a beat later (native accept, or the next turn). Any
  // `/api/chats` poll or WS reconnect in that window (`reconcileChatList`
  // overwrites `chats.value` with the server snapshot, then `loadMessages` runs
  // `rebuildPendingQuestion`) would otherwise resurrect the answered picker —
  // and because `rebuildPendingQuestion` bails when a picker is already live, a
  // later clean snapshot never removes it, so the card sticks. Remembering the
  // resolved signature lets `rebuildPendingQuestion` refuse the stale rebuild.
  const resolvedQuestions = ref<Record<string, Set<string>>>({})

  // Stable identity for a picker, computable identically from the live
  // `activeQuestions` entry (at resolve time) and from a rebuilt `pending_question`
  // (at rebuild time). Some providers carry a `requestId`; Claude's
  // picker has none, so fall back to the question content.
  function questionsSignature(qs: ActiveQuestion[] | undefined): string {
    if (!qs || !qs.length) return ''
    const rid = qs[0]?.requestId
    if (rid) return `rid:${rid}`
    return `q:${qs.map(q => `${q.id}${q.question}`).join('')}`
  }

  // Record the currently-active picker for `chatId` as resolved. Reads the live
  // `activeQuestions` entry, so it must run before that entry is deleted.
  function markResolvedQuestion(chatId: string) {
    const sig = questionsSignature(activeQuestions.value[chatId])
    if (!sig) return
    ;(resolvedQuestions.value[chatId] ||= new Set<string>()).add(sig)
  }

  // Parse the AskUserQuestion tool_input JSON (`{"questions": [...]}`) into the
  // picker's shape. Shared by the live `tool_use` handler and the reload-time
  // rebuild from a chat's persisted `pending_question`. Returns [] on anything
  // unparseable so callers can fall through to the generic trace path.
  function parseQuestions(
    toolInput: string | null | undefined,
    requestId = '',
  ): ActiveQuestion[] {
    if (!toolInput) return []
    try {
      const parsed = JSON.parse(toolInput)
      if (!Array.isArray(parsed?.questions)) return []
      const resolvedRequestId = requestId || String(parsed?.request_id ?? '')
      if (parsed.questions.length === 0) {
        // Some provider turns emit the AskUserQuestion tool
        // with an empty questions array. Do not silently demote that event to
        // a trace row: surface a free-form response so the user can unblock
        // the turn and the provider still receives the native request id.
        return [{
          id: '__freeform__',
          question: 'The model needs your input. Enter a response to continue.',
          header: 'Response',
          multiSelect: false,
          allowOther: true,
          isSecret: false,
          requestId: resolvedRequestId,
          options: [],
        }]
      }
      // Claude Code's documented AskUserQuestion shape uses
      // `question`/`header`/`multiSelect`. Some providers (seen with
      // MiniMax via the Claude path) emit an alternate shape with
      // `text`/`type: single_select|multi_select` instead — accept both
      // so the picker prompt is never blank when the model did ask.
      return parsed.questions.map((q: Record<string, unknown>, index: number) => {
        const type = String(q.type ?? '').toLowerCase()
        return {
          id: String(q.id ?? index),
          question: String(q.question ?? q.text ?? ''),
          header: String(q.header ?? q.title ?? ''),
          multiSelect: Boolean(q.multiSelect) || type === 'multi_select',
          allowOther: q.isOther === undefined
            ? true
            : Boolean(q.isOther) || !Array.isArray(q.options) || q.options.length === 0,
          isSecret: Boolean(q.isSecret),
          requestId: resolvedRequestId,
          options: Array.isArray(q.options)
            ? (q.options as Array<Record<string, unknown>>).map(o => ({
                label: String(o.label ?? o.value ?? ''),
                description: o.description ? String(o.description) : '',
              }))
            : [],
        }
      })
    } catch {
      return []
    }
  }

  // ── Image-capability questions ────────────────────────────────────────
  // Rendered when the engine pre-flights an image turn and the selected
  // model cannot see images; the user picks a vision-capable model (switch),
  // opens the full model picker, or cancels. Answered with a
  // `capability_response` client message. Unlike `activeQuestions` there is
  // no persisted copy on the chat — the question lives only for the
  // in-flight turn, so it is never rebuilt on reload.
  type CapabilityCandidate = {
    id: string
    label: string
    supports_vision?: boolean
    disabled?: boolean
  }
  type CapabilityQuestion = {
    request_id: string
    missing: string
    current_model: string
    candidates: CapabilityCandidate[]
    timeout_s: number
    opened_at: number
  }
  const activeCapabilityQuestions = ref<Record<string, CapabilityQuestion[]>>({})

  function parseCapabilityQuestion(event: {
    request_id: string
    missing?: string
    current_model?: string
    candidates?: Array<Record<string, unknown>>
    timeout_s?: number
  }): CapabilityQuestion {
    return {
      request_id: event.request_id,
      missing: String(event.missing ?? 'image_input'),
      current_model: String(event.current_model ?? ''),
      candidates: Array.isArray(event.candidates)
        ? (event.candidates as Array<Record<string, unknown>>).map(c => ({
            id: String(c.id ?? ''),
            label: String(c.label ?? c.id ?? ''),
            supports_vision:
              c.supports_vision === undefined ? undefined : Boolean(c.supports_vision),
            disabled: Boolean(c.disabled),
          }))
        : [],
      timeout_s: Number(event.timeout_s ?? 30),
      opened_at: Date.now(),
    }
  }

  // Restore the AskUserQuestion picker after a reload. The picker lives in
  // ephemeral `activeQuestions` (set only by the live stream), but the server
  // persists the unanswered question on the chat, so we rebuild from there on
  // chat open. Never clobbers a picker already populated by the live stream.
  function rebuildPendingQuestion(chatId: string) {
    if (activeQuestions.value[chatId]?.length) return
    const chat = chats.value.find(c => c.chat_id === chatId)
    const qs = parseQuestions(chat?.pending_question)
    if (!qs.length) return
    // Don't resurrect a picker the user already answered/dismissed from a
    // server snapshot that hasn't caught up yet.
    if (resolvedQuestions.value[chatId]?.has(questionsSignature(qs))) return
    activeQuestions.value[chatId] = qs
  }

  // Same idea as `rebuildPendingQuestion`, for the Approve/Deny card: it
  // lives in ephemeral `pendingPermissions` (set only by the live stream),
  // but the server persists the unanswered request on the chat so a chat
  // opened after the prompt already fired (reload, other device, chat
  // switch) still shows the card instead of nothing.
  function rebuildPendingPermission(chatId: string) {
    const chat = chats.value.find(c => c.chat_id === chatId)
    const raw = chat?.pending_permission
    if (!raw) return
    let parsed: { request_id?: string; tool_name?: string; message?: string; tool_input?: string }
    try {
      parsed = JSON.parse(raw)
    } catch {
      return
    }
    if (!parsed.request_id) return
    const list = pendingPermissions.value[chatId] || []
    if (list.some(p => p.request_id === parsed.request_id)) return
    pendingPermissions.value[chatId] = [
      ...list,
      {
        request_id: parsed.request_id,
        tool_name: parsed.tool_name || '',
        tool_input: parsed.tool_input || '',
        message: parsed.message || '',
        received_at: Date.now(),
      },
    ]
  }
  const eventsSocket = ref<WebSocket | null>(null)
  const toasts = ref<InAppToast[]>([])
  let toastCounter = 0
  const packageStatus = ref<PackageStatus | null>(null)

  // Reactive mirror of document.visibilityState so `chatUnread` (and any other
  // computed that cares about foreground/background) re-evaluates correctly
  // on tab/app switches. Kept in sync by the visibilitychange listener below.
  const documentVisible = ref(
    typeof document !== 'undefined' ? document.visibilityState === 'visible' : true
  )
  let latestSyncInFlight = false

  // ── WebSocket liveness ──────────────────────────────────────────────
  // The server sends a `keepalive` frame on both /ws/chat and /ws/events
  // every STREAM_KEEPALIVE_SECONDS (5s, see ciao/web/chat_broker.py). We use
  // those frames purely as a liveness signal: a socket that reports
  // readyState OPEN but has received nothing for well over the keepalive
  // cadence is half-open (common after iOS/WKWebView suspend or a flaky
  // network) and will never fire `onclose`, so results/subagent events
  // published server-side never arrive and the UI looks hung until the user
  // sends a message. The watchdog below force-reconnects such sockets.
  const WS_STALE_MS = 12000 // ~2 missed keepalives + margin
  const WS_LIVENESS_CHECK_MS = 2000
  // Cheap GET; frequent enough that a background-app-store style badge
  // reflects reality without checking on every render or route change.
  const UPDATE_CHECK_INTERVAL_MS = 30 * 60 * 1000
  let lastEventsFrameAt = 0
  const lastChatFrameAt: Record<string, number> = {}
  const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

  // ── Unacknowledged sends ────────────────────────────────────────────
  // A send is handed to the per-chat WebSocket fire-and-forget: WS frames
  // have no delivery guarantee, and WKWebView suspension can close the
  // socket right after the send frame is written (the server log shows the
  // message frame arriving and the CLOSE frame in the same instant). The
  // server never started a turn, the optimistic bubble is the only copy,
  // and the reconnect's authoritative history reload wipes it — the
  // "page refreshed and removed my message" report. Track the latest send
  // per chat until the server proves receipt (a user_echo replay, or the
  // turn visible in /messages) and re-send it once on reconnect if it
  // never landed. Only the most recent send is tracked: an earlier frame
  // in the same burst to a dying socket is rare next to the common
  // single-message case, and the queue path already persists server-side
  // when its frame arrives.
  interface UnackedSend { text: string; images?: string[]; at: number; attempts: number }
  const unackedSends: Record<string, UnackedSend> = {}
  const unackedRecoveryTimers: Record<string, number> = {}
  // Grace before declaring a send lost: a turn that DID start replays its
  // buffered user_echo immediately after reconnect, and /messages can lag
  // the provider session write by a moment.
  const UNACKED_RECOVERY_DELAY_MS = 1500

  function persistUnackedSends() {
    try {
      localStorage.setItem('ciao-unacked-sends', JSON.stringify(unackedSends))
    } catch { /* ignore */ }
  }

  // Server proof of receipt: the echoed turn (user_echo) or the hydrated
  // history row. Whichever lands first clears the tracking.
  function acknowledgeSend(chatId: string, text: string) {
    const unacked = unackedSends[chatId]
    if (unacked && unacked.text.trim() === text.trim()) {
      delete unackedSends[chatId]
      persistUnackedSends()
    }
  }

  function reconcileUnackedSend(chatId: string) {
    const unacked = unackedSends[chatId]
    if (!unacked) return
    const rows = messages.value[chatId] || []
    // Only a SERVER-stamped row proves delivery: hydrated user bubbles carry
    // the server-assigned turn_index, while our own optimistic bubble (which
    // can survive a history reload against an older server, or simply look
    // identical to an older repeated message) never has one.
    if (rows.some(m => m.role === 'user' && m.turn_index != null && (m.content || '').trim() === unacked.text.trim())) {
      delete unackedSends[chatId]
      persistUnackedSends()
    }
  }

  // Called after a reconnect's history reload. Waits out the grace window,
  // then either recovers the send or gives up visibly — never silently.
  function scheduleUnackedSendRecovery(chatId: string) {
    if (!unackedSends[chatId]) return
    if (unackedRecoveryTimers[chatId]) window.clearTimeout(unackedRecoveryTimers[chatId])
    unackedRecoveryTimers[chatId] = window.setTimeout(() => {
      delete unackedRecoveryTimers[chatId]
      recoverUnackedSend(chatId)
    }, UNACKED_RECOVERY_DELAY_MS)
  }

  function recoverUnackedSend(chatId: string) {
    reconcileUnackedSend(chatId)
    const unacked = unackedSends[chatId]
    if (!unacked) return
    // A running turn owns the answer: either the original frame landed and
    // the echo/history checks raced it, or a newer send already started.
    // Never duplicate into a server-reported active stream. The client-local
    // optimistic `streaming` flag cannot veto here — after a lost message no
    // result ever arrives to clear it, so it would block recovery forever.
    if (projectStreaming.value[chatId]) return
    if (unacked.attempts >= 1) {
      // One silent recovery is enough; surface the failure rather than loop.
      delete unackedSends[chatId]
      persistUnackedSends()
      const errorMsgs = messages.value[chatId] || []
      errorMsgs.push({
        role: 'system',
        content: "Error: a message didn't reach the engine and its automatic retry failed. Please send it again.",
        timestamp: new Date().toISOString(),
      })
      messages.value[chatId] = errorMsgs
      return
    }
    unacked.attempts += 1
    persistUnackedSends()
    // The stale optimistic streaming state belongs to a turn that never
    // started; leaving it set would route this resend into the local queue
    // (no server stream will ever drain it) instead of starting a real turn.
    clearStreamingState(chatId)
    // The optimistic bubble from the lost send is gone with the history
    // reload, so this re-renders it fresh; a successful delivery clears the
    // tracking via the echoed user_echo. Empty comment buckets: the original
    // send already consumed them via consumePreparedAttachments.
    sendMessage(chatId, unacked.text, {
      composed: unacked.text,
      imageRefs: unacked.images,
      fileComments: [],
      chatComments: [],
    })
  }

  // Per-chat WS auto-reconnect bookkeeping. A dropped per-chat socket used to
  // recover only via the 15s syncLatest poll (up to 15s of stale messages /
  // missed turn result). We now reconnect the *active* chat immediately on an
  // unexpected close, with backoff. `intentionalCloses` marks a close made by
  // disconnectWs so it is NOT auto-reconnected; `chatReconnectTimers` lets a
  // pending reconnect be cancelled; attempts drive the backoff and reset once
  // the socket proves live (first frame received).
  const intentionalCloses = new Set<WebSocket>()
  const chatReconnectTimers: Record<string, number> = {}
  const chatReconnectAttempts: Record<string, number> = {}
  // After an unexpected drop or half-open recovery, keep the frozen Activity
  // timeline on screen and rebuild it from the broker replay on the first
  // non-keepalive frame so the UI does not blank mid-turn.
  const pendingStreamResync = new Set<string>()

  // ── Computed ─────────────────────────────────────────────────────────

  const workspaceProjects = computed(() =>
    projects.value
      .filter(p => p.workspace === activeWorkspace.value)
      .sort((a, b) => a.order - b.order || a.name.localeCompare(b.name))
  )

  const workspaceOptions = computed<WorkspaceInfo[]>(() => {
    if (workspaces.value.length) return workspaces.value
    const names = Array.from(new Set(projects.value.map(p => p.workspace).filter(Boolean)))
    if (names.length) {
      return names.map(name => ({
        name,
        vault_root: '',
        default_provider: 'claude',
        default_model: '',
        gws_profile: '',
      }))
    }
    return [
      { name: 'personal', vault_root: 'personal', default_provider: 'claude', default_model: '', gws_profile: 'personal' },
      { name: 'work', vault_root: 'work', default_provider: 'claude', default_model: '', gws_profile: 'work' },
    ]
  })

  const activeChat = computed(() =>
    chats.value.find(c => c.chat_id === activeChatId.value) || null
  )

  const activeProject = computed(() => {
    const chat = activeChat.value
    if (!chat) return null
    return projects.value.find(p => p.project_id === chat.project_id) || null
  })

  const activeMessages = computed(() =>
    messages.value[activeChatId.value || ''] || []
  )

  const messageHistoryLoading = computed(() =>
    Boolean(loadingMessages.value[activeChatId.value || ''])
  )

  const activeSubagents = computed<SubagentTranscript[]>(() =>
    subagents.value[activeChatId.value || ''] || []
  )

  // True while the active chat has a live turn. Includes `projectStreaming`
  // (events-WS server truth) so a mid-turn `/messages` poll that hydrates
  // progress text cannot tear down the Working... Activity and promote a
  // half-written note into the reply bubble.
  const isStreaming = computed(() => {
    const chatId = activeChatId.value || ''
    return Boolean(streaming.value[chatId] || projectStreaming.value[chatId])
  })

  const currentStreamingText = computed(() =>
    streamingText.value[activeChatId.value || ''] || ''
  )

  const currentStreamingThinking = computed(() =>
    streamingThinking.value[activeChatId.value || ''] || ''
  )

  const currentQueued = computed(() =>
    queuedMessages.value[activeChatId.value || ''] || []
  )

  const activeBackgroundAgents = computed(() =>
    backgroundAgents.value[activeChatId.value || ''] || 0
  )

  // Live view while subagents run: refresh the active chat's subagent
  // transcripts on a short interval so the panel updates as the agents
  // work. The CLI appends to the transcript files continuously, so polling
  // the REST endpoint is enough for a near-live feed. Runs while the active
  // chat has running background agents OR is streaming a turn (agents
  // dispatched mid-turn nest live inside the Working trace).
  let subagentPollTimer: ReturnType<typeof setInterval> | null = null
  watch(
    () => [activeChatId.value, activeBackgroundAgents.value, isStreaming.value] as const,
    ([chatId, count, streamingNow]) => {
      if (subagentPollTimer !== null) {
        clearInterval(subagentPollTimer)
        subagentPollTimer = null
      }
      if (!chatId || (count <= 0 && !streamingNow)) return
      subagentPollTimer = setInterval(() => {
        void loadSubagents(chatId)
      }, 4000)
    },
  )

  function projectChats(projectId: string): ChatInfo[] {
    // Hide remote chats (session lives on another device, not openable here).
    return chats.value
      .filter(c => c.project_id === projectId && !c.archived && c.local !== false)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
  }

  // Sidebar ordering: every supervisor immediately followed by its delegates,
  // which render indented. Deliberately separate from projectChats, which stays
  // the flat list the counts (unread, needs-input) are summed over — a
  // delegate's unread still belongs to its project.
  function projectChatRows(projectId: string): ChatRow[] {
    return projectChatGroups(projectId).flatMap(group => [
      { chat: group.chat, isDelegate: false },
      ...group.delegates.map(chat => ({ chat, isDelegate: true })),
    ])
  }

  function projectChatGroups(projectId: string): ChatGroup[] {
    const all = projectChats(projectId)
    const visible = new Set(all.map(c => c.chat_id))
    const byParent = new Map<string, ChatInfo[]>()
    for (const chat of all) {
      const parent = chat.spawned_from_chat_id
      if (!parent || !visible.has(parent)) continue
      const siblings = byParent.get(parent) || []
      siblings.push(chat)
      byParent.set(parent, siblings)
    }
    const groups: ChatGroup[] = []
    for (const chat of all) {
      // A delegate nests only when its supervisor is visible in this same
      // project. Orphans (supervisor archived, deleted, or moved elsewhere)
      // stay top-level rather than vanishing from the sidebar entirely.
      if (chat.spawned_from_chat_id && visible.has(chat.spawned_from_chat_id)) continue
      groups.push({ chat, delegates: byParent.get(chat.chat_id) || [] })
    }
    return groups
  }

  function chatActivity(chat: ChatInfo): string {
    return chat.last_activity_at || chat.created_at
  }

  // Built once per chats mutation so the delegate filters below stay linear
  // instead of scanning the whole list for every chat's supervisor.
  const chatsById = computed(() => new Map(chats.value.map(c => [c.chat_id, c])))

  // True when this chat is a nested delegate whose supervisor is still a
  // visible (non-archived, local) chat. Used to hide subchats from home /
  // recent surfaces so you jump back into the supervisor and reach children
  // from there. Orphans (supervisor archived/missing) stay listed.
  function isNestedDelegate(chat: ChatInfo): boolean {
    const parentId = chat.spawned_from_chat_id
    if (!parentId) return false
    const parent = chatsById.value.get(parentId)
    return Boolean(parent && !parent.archived && parent.local !== false)
  }

  // A chat's active delegate subchats, as the user can actually see them.
  //
  // One definition, because the same filter had drifted into three copies and
  // two of them dropped the `local !== false` guard that every other
  // visibility computation applies — so an archive button offered to take "2
  // subchats" while the sidebar listed 1. Counts shown to the user must match
  // the rows they can see. The server-side cascade deliberately covers remote
  // delegates too; that asymmetry is intentional, not a bug to paper over here.
  function activeDelegatesFor(chatId: string): ChatInfo[] {
    return chats.value.filter(
      c => c.spawned_from_chat_id === chatId && !c.archived && c.local !== false,
    )
  }

  // Most recent (max 5) non-archived chats in the active workspace.
  const recentChats = computed<ChatInfo[]>(() => {
    const wsProjectIds = new Set(workspaceProjects.value.map(p => p.project_id))
    return chats.value
      .filter(c => !c.archived && c.local !== false && wsProjectIds.has(c.project_id))
      .filter(c => !isNestedDelegate(c))
      .filter(c => Boolean(chatActivity(c)))
      .sort((a, b) => chatActivity(b).localeCompare(chatActivity(a)))
      .slice(0, 5)
  })

  // Full "jump back in" list for the home screen: every non-archived local
  // chat with activity, across ALL workspaces, newest first (uncapped). The
  // home surface is a global hub, so unlike recentChats it isn't scoped to
  // the active workspace — each chat carries its own workspace/project tag.
  // Nested delegates are omitted; open the supervisor to reach them.
  const activeChatsAll = computed<ChatInfo[]>(() => {
    return chats.value
      .filter(c => !c.archived && c.local !== false)
      .filter(c => !isNestedDelegate(c))
      .filter(c => Boolean(chatActivity(c)))
      .sort((a, b) => chatActivity(b).localeCompare(chatActivity(a)))
  })

  function isChatStreaming(chatId: string): boolean {
    return Boolean(projectStreaming.value[chatId] || streaming.value[chatId])
  }

  // Background subagents outlive the turn that spawned them; this powers the
  // sidebar/header indicators during the quiet gap where no turn is
  // streaming but agents are still working.
  function chatHasBackgroundAgents(chatId: string): boolean {
    return (backgroundAgents.value[chatId] || 0) > 0
  }

  // True when a delegate nested under this chat (or under one of its own
  // delegates) is streaming or has background agents. A supervisor chat with
  // no live turn of its own still has real work in flight while a delegate is
  // busy, and the home lanes / per-row signal only checked the chat's own
  // state — a project full of working subchats read as "quiet".
  function chatHasActiveDelegates(chatId: string, seen: Set<string> = new Set()): boolean {
    if (seen.has(chatId)) return false
    seen.add(chatId)
    return activeDelegatesFor(chatId).some(
      d => isChatStreaming(d.chat_id) || chatHasBackgroundAgents(d.chat_id) || chatHasActiveDelegates(d.chat_id, seen),
    )
  }

  // ── Post-archive pipeline ────────────────────────────────────────────────
  // Archiving a chat starts insights extraction, a project-doc fold, a
  // trajectory and memory proposals. The state lives on the chat itself (so an
  // archived chat can still report what was learned from it after a reload);
  // these are the read paths every surface shares.

  function chatPostprocess(chatId: string): ChatPostprocess | null {
    return chats.value.find(c => c.chat_id === chatId)?.postprocess || null
  }

  function chatIsPostprocessing(chatId: string): boolean {
    return isPostprocessing(chatPostprocess(chatId))
  }

  // Archived chats matching a predicate, newest archive first. Shared by
  // postprocessingChats/insightsFailedChats so both stay consistent with
  // their *Count siblings below. Archived chats are excluded from
  // activeChatsAll, so this is the one path that surfaces them while the
  // pipeline runs.
  function chatsMatching(predicate: (chat: ChatInfo) => boolean): ChatInfo[] {
    return chats.value
      .filter(predicate)
      .sort((a, b) =>
        (b.last_activity_at || b.created_at).localeCompare(a.last_activity_at || a.created_at),
      )
  }

  /** Count of one workspace's chats matching a predicate, for lane headers. */
  function workspaceCountMatching(ws: WorkspaceName, predicate: (chat: ChatInfo) => boolean): number {
    const wsProjectIds = new Set(
      projects.value.filter(p => p.workspace === ws).map(p => p.project_id),
    )
    return chats.value.filter(c => wsProjectIds.has(c.project_id) && predicate(c)).length
  }

  /** Chats being tidied up in a workspace, for the home lane summary. */
  function workspacePostprocessingCount(ws: WorkspaceName): number {
    return workspaceCountMatching(ws, c => isPostprocessing(c.postprocess))
  }

  function postprocessingChats(): ChatInfo[] {
    return chatsMatching(c => isPostprocessing(c.postprocess))
  }

  /** Archived chats whose insights extraction failed and can be retried. */
  function insightsFailedChats(): ChatInfo[] {
    return chatsMatching(c => postprocessNeedsInsights(c.postprocess))
  }

  /** Insights-failed count for one workspace, for the home lane header. */
  function workspaceInsightsFailedCount(ws: WorkspaceName): number {
    return workspaceCountMatching(ws, c => postprocessNeedsInsights(c.postprocess))
  }

  function projectPostprocessingCount(projectId: string): number {
    return chats.value.filter(
      c => c.project_id === projectId && isPostprocessing(c.postprocess),
    ).length
  }

  // ── Archiving (optimistic) ────────────────────────────────────────────
  // Chats whose archive POST is in flight. They are already marked
  // `archived:true` optimistically (so they vanish from active lists / the
  // sidebar) but are listed in the home "archiving…" queue until the server
  // confirms. A failed POST rolls `archived` back and clears the entry.
  function isArchiving(chatId: string): boolean {
    return Boolean(archivingChats.value[chatId])
  }

  function archivingChatsList(): ChatInfo[] {
    return chatsMatching(c => Boolean(archivingChats.value[c.chat_id]))
  }

  function workspaceArchivingCount(ws: WorkspaceName): number {
    return workspaceCountMatching(ws, c => Boolean(archivingChats.value[c.chat_id]))
  }

  function projectArchivingCount(projectId: string): number {
    return chats.value.filter(
      c => c.project_id === projectId && Boolean(archivingChats.value[c.chat_id]),
    ).length
  }

  /**
   * Reconcile against the server's list of live pipelines. A chat the server
   * omits has settled: downgrade it to 'done' rather than dropping the record,
   * because the outcomes it already collected are still worth showing.
   */
  function applyPostprocessingSnapshot(runningIds: string[]): void {
    const running = new Set(runningIds)
    for (const chat of chats.value) {
      const pp = chat.postprocess
      if (!pp) continue
      if (pp.state === 'running' && !running.has(chat.chat_id)) {
        chat.postprocess = { ...pp, state: 'done', step: '' }
      }
    }
  }

  function projectIsStreaming(projectId: string): boolean {
    // Same visibility rules as projectChats: a chat hidden from the sidebar
    // (archived or remote) must never light the project header dot.
    return chats.value.some(
      c => c.project_id === projectId && !c.archived && c.local !== false && isChatStreaming(c.chat_id),
    )
  }

  function workspaceIsStreaming(ws: WorkspaceName): boolean {
    // Compose the project-level check so both dots follow the same
    // visibility rules — a hidden chat must not light either level.
    return projects.value.some(p => p.workspace === ws && projectIsStreaming(p.project_id))
  }

  function projectFor(chatId: string): ProjectInfo | null {
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat) return null
    return projects.value.find(p => p.project_id === chat.project_id) || null
  }

  // ── Toasts ──────────────────────────────────────────────────────────

  function pushToast(toast: Omit<InAppToast, 'id'>): InAppToast {
    const t: InAppToast = { id: ++toastCounter, ...toast }
    toasts.value.push(t)
    // Notifications auto-dismiss; error toasts persist until dismissed or acted on.
    if (t.variant !== 'error') {
      setTimeout(() => dismissToast(t.id), 5000)
    }
    return t
  }

  // Surface a failure as a persistent, actionable error toast. `errorText` is
  // the raw log seeded into a fix chat when the user clicks "Fix". For errors
  // whose remediation lives in Settings, pass opts.fixRoute so the Fix action
  // navigates there instead of opening a fix chat.
  function pushErrorToast(
    title: string,
    errorText: string,
    opts?: { fixRoute?: string; fixLabel?: string },
  ): InAppToast {
    return pushToast({
      chat_id: '',
      title,
      body: errorText,
      variant: 'error',
      errorText,
      ...opts,
    })
  }

  function dismissToast(id: number) {
    const idx = toasts.value.findIndex(t => t.id === id)
    if (idx >= 0) toasts.value.splice(idx, 1)
  }

  // Keyed by version so the toast fires once per newly-available release, not
  // once per session for an update the user already knows about and hasn't
  // installed yet. The Settings nav badge (ProjectSidebar.vue) is intentionally
  // not gated the same way: it just mirrors packageStatus.update_available for
  // as long as that stays true, the way the bell badge mirrors unread count.
  const UPDATE_TOAST_SEEN_KEY = 'ciao-update-toast-seen-version'

  async function checkPackageStatus() {
    try {
      const status = await api.get<PackageStatus>('/api/package/status')
      packageStatus.value = status
      if (status.update_available && status.latest_version) {
        const seen = typeof localStorage !== 'undefined'
          ? localStorage.getItem(UPDATE_TOAST_SEEN_KEY)
          : null
        if (seen !== status.latest_version) {
          pushToast({
            chat_id: '',
            title: 'Update available',
            body: `Ciaobot ${status.latest_version} is ready to install — see Settings.`,
          })
          if (typeof localStorage !== 'undefined') {
            localStorage.setItem(UPDATE_TOAST_SEEN_KEY, status.latest_version)
          }
        }
      }
    } catch {
      // Best-effort: the update badge just stays off if the check fails.
    }
  }

  // Open a fresh chat in the active workspace's auto-managed General project,
  // pre-filled with a prompt asking the agent to diagnose and fix `errorText`
  // (falling back to a GitHub issue if the bug is in Ciaobot itself).
  // The active workspace's auto-managed General project, or null if absent.
  // Shared by fixError and the Cmd+T "new chat in General" shortcut.
  function generalProject(workspace: WorkspaceName = activeWorkspace.value) {
    return (
      projects.value.find(
        p => p.workspace === workspace && p.is_auto && p.name === 'General',
      ) ?? null
    )
  }

  async function fixError(opts: {
    errorText: string
    context?: string
    title?: string
  }): Promise<ChatInfo | undefined> {
    const general = generalProject()
    if (!general) {
      pushErrorToast(
        'Cannot open fix chat',
        'No General project found in this workspace to open a fix chat in.',
      )
      return
    }
    const chat = await createChat(general.project_id, opts.title || 'Fix error')
    const prompt = buildFixPrompt({ errorText: opts.errorText, context: opts.context })
    await sendMessage(chat.chat_id, prompt)
    return chat
  }

  // Cmd+T: open a fresh, empty chat in the default General project.
  async function newChatInGeneral(): Promise<ChatInfo | undefined> {
    const general = generalProject()
    if (!general) {
      pushErrorToast('Cannot open a new chat', 'No General project found in this workspace.')
      return
    }
    return createChat(general.project_id)
  }

  // Recover a draft orphaned by a server-side empty-chat sweep (#277): open a
  // fresh chat pre-filled with the recovered text, in its original project
  // when that still exists, otherwise General. Only ever called from an
  // explicit user "Restore" click — never speculatively — so a chat is only
  // created when the user actually asked for one.
  async function restoreDraft(payload: {
    originalChatId: string
    projectId: string
    text: string
    workspace?: string
  }) {
    const project =
      projects.value.find(p => p.project_id === payload.projectId) ??
      generalProject((payload.workspace as WorkspaceName) || activeWorkspace.value)
    if (!project) throw new Error('No project available to restore into')
    if (project.workspace !== activeWorkspace.value) {
      await switchWorkspace(project.workspace)
    }
    await createChat(project.project_id, DEFAULT_CHAT_TITLE, payload.text)
    clearChatDraft(payload.originalChatId)
  }

  // ── Persistence ─────────────────────────────────────────────────────

  function stripLegacyContextPrefix(content: string): string {
    const lines = content.split('\n')
    let idx = 0
    let seenContext = false

    while (idx < lines.length) {
      const line = lines[idx]
      if (!line.trim()) {
        if (seenContext) {
          const remainder = lines.slice(idx + 1).join('\n').trim()
          return remainder || content
        }
        idx += 1
        continue
      }
      if (
        line.startsWith('[CONTEXT: ') ||
        line.startsWith('[Project context: ') ||
        line.startsWith('[Project: "') ||
        line.startsWith('[Chat: "')
      ) {
        seenContext = true
        idx += 1
        continue
      }
      if (line.startsWith('[PWA interface: ')) {
        seenContext = true
        idx += 1
        while (idx < lines.length) {
          if (lines[idx].endsWith('space.]')) {
            idx += 1
            break
          }
          idx += 1
        }
        continue
      }
      break
    }

    if (seenContext) {
      while (idx < lines.length && !lines[idx].trim()) idx += 1
      const remainder = lines.slice(idx).join('\n').trim()
      return remainder || content
    }

    return content
  }

  // Mirror of ciao/web/routes_api.py:_IMAGE_MANIFEST_RE. `build_prompt()` in
  // ciao/providers/base.py appends an "[INCOMING IMAGES]\n1. filename.png"
  // manifest to the user's text before sending to the SDK. The SDK persists
  // it in the session file, so it leaks into replayed history. The UI renders
  // images separately from `msg.images`, so the manifest is redundant.
  const IMAGE_MANIFEST_RE = /\n{0,2}\[INCOMING IMAGES\]\n(?:\d+\. [^\n]*(?:\n|$))+\s*$/

  function stripImageManifest(content: string): string {
    const stripped = content.replace(IMAGE_MANIFEST_RE, '')
    return stripped || content
  }

  function sanitizeInjectedContext(content: string): string {
    const beginMarker = '[CIAO_CONTEXT_BEGIN]\n'
    const endMarker = '\n[CIAO_CONTEXT_END]\n\n'
    if (content.startsWith(beginMarker)) {
      const endIndex = content.indexOf(endMarker)
      if (endIndex >= 0) {
        const stripped = content.slice(endIndex + endMarker.length).trim()
        return stripImageManifest(stripped).trim() || content
      }
    }
    const legacy = stripImageManifest(stripLegacyContextPrefix(content))
    return legacy.trim() || content
  }

  function normalizeMessages(chatMessages: ChatMessage[]): ChatMessage[] {
    return chatMessages
      .map((message) => {
        let content = message.content || ''
        if (message.role === 'user') content = sanitizeInjectedContext(content)
        content = content.trim()
        return { ...message, content }
      })
      .filter((message) => {
        // Remove cached bubbles written by older clients. The live proxy event
        // now drives one ephemeral connection card outside chat history.
        if (
          message.role === 'system'
          && isHostConnectionUnavailableMessage(
            message.content.replace(/^Error:\s*/i, ''),
          )
        ) return false
        if (message.tool_name === '_activity') return Boolean(message.content)
        if (message.tool_name === '_filecard') {
          return Boolean(message.file_path) && isPlausibleFilePath(message.file_path || '')
        }
        if (message.role === 'system') return Boolean(message.content)
        return Boolean(message.content)
      })
  }

  function userMessageIncludesQueuedText(content: string, queuedText: string): boolean {
    const queued = queuedText.trim()
    if (!queued) return false
    const rendered = content.trim()
    if (rendered === queued) return true
    return rendered.split(/\n{2,}/).some(part => part.trim() === queued)
  }

  function queuedTextAlreadyRendered(chatMessages: ChatMessage[], queuedText: string): boolean {
    return chatMessages.some(
      m => m.role === 'user' && userMessageIncludesQueuedText(m.content, queuedText),
    )
  }

  function reconcileQueuedWithMessages(chatId: string) {
    const list = queuedMessages.value[chatId]
    if (!list?.length) return
    const chatMessages = messages.value[chatId] || []
    const remaining = list.filter(q => !queuedTextAlreadyRendered(chatMessages, q.text))
    if (remaining.length) queuedMessages.value[chatId] = remaining
    else delete queuedMessages.value[chatId]
  }

  function historySignature(chatMessages: ChatMessage[]): string {
    return JSON.stringify(
      chatMessages
        .filter(m => m.tool_name !== '_thinking')
        .map((message) => ({
          role: message.role,
          content: message.content,
          tool_name: message.tool_name || '',
          is_error: Boolean(message.is_error),
          phase: message.phase || '',
        }))
    )
  }

  // The server rebuilds /api/chats/:id/messages from the raw SDK session
  // file, which preserves role/content/tools but NOT the ResultEvent
  // metadata (usage, effective_model, is_error). When loadMessages adopts
  // the server version, overlay that metadata from matching local
  // messages so post-reconcile the context % (context_pct lives inside
  // usage) doesn't evaporate.
  function mergeMessageFields(sMsg: ChatMessage, lMsg: ChatMessage): ChatMessage {
    const merged: ChatMessage = { ...sMsg }
    if (lMsg.usage && !sMsg.usage) merged.usage = lMsg.usage
    if (lMsg.quota && !sMsg.quota) merged.quota = lMsg.quota
    if (lMsg.effective_model && !sMsg.effective_model) merged.effective_model = lMsg.effective_model
    if (lMsg.is_error !== undefined && sMsg.is_error === undefined) merged.is_error = lMsg.is_error
    if (lMsg.turn_index != null && sMsg.turn_index == null) merged.turn_index = lMsg.turn_index
    if (lMsg.duration_ms != null && sMsg.duration_ms == null) merged.duration_ms = lMsg.duration_ms
    // Loop/schedule marker observed live but missing on the server row (older
    // servers, or a row built before the turn was recorded) — keep the ↻.
    if (lMsg.unattended && !sMsg.unattended) merged.unattended = lMsg.unattended
    if (!merged.timestamp && lMsg.timestamp) merged.timestamp = lMsg.timestamp
    return merged
  }

  function groupIntoTurns(msgsList: ChatMessage[]): { user: ChatMessage | null; responses: ChatMessage[] }[] {
    const turns: { user: ChatMessage | null; responses: ChatMessage[] }[] = []
    let currentTurn: { user: ChatMessage | null; responses: ChatMessage[] } = { user: null, responses: [] }
    for (const m of msgsList) {
      if (m.role === 'user') {
        if (currentTurn.user || currentTurn.responses.length) {
          turns.push(currentTurn)
        }
        currentTurn = { user: m, responses: [] }
      } else {
        currentTurn.responses.push(m)
      }
    }
    if (currentTurn.user || currentTurn.responses.length) {
      turns.push(currentTurn)
    }
    return turns
  }

  function mergeMetadata(server: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
    const serverTurns = groupIntoTurns(server)
    const localTurns = groupIntoTurns(local)
    const mergedMessages: ChatMessage[] = []

    for (let i = 0; i < serverTurns.length; i++) {
      const sTurn = serverTurns[i]
      const lTurn = localTurns[i]
      const matches = lTurn && (
        (!sTurn.user && !lTurn.user) ||
        (sTurn.user && lTurn.user && sTurn.user.content === lTurn.user.content)
      )

      if (!matches) {
        if (sTurn.user) mergedMessages.push(sTurn.user)
        mergedMessages.push(...sTurn.responses)
      } else {
        if (sTurn.user && lTurn.user) {
          mergedMessages.push(mergeMessageFields(sTurn.user, lTurn.user))
        }

        const mergedResponses: ChatMessage[] = []
        const sAssistantMsgs = sTurn.responses.filter(m => m.role === 'assistant' && !m.tool_name)
        let sAsstIdx = 0

        for (const lMsg of lTurn.responses) {
          if (lMsg.role === 'assistant' && !lMsg.tool_name) {
            const sMsg = sAssistantMsgs[sAsstIdx]
            if (sMsg) {
              mergedResponses.push(mergeMessageFields(sMsg, lMsg))
              sAsstIdx++
            }
          } else {
            mergedResponses.push(lMsg)
          }
        }
        for (let j = sAsstIdx; j < sAssistantMsgs.length; j++) {
          mergedResponses.push(sAssistantMsgs[j])
        }
        mergedMessages.push(...mergedResponses)
      }
    }
    return mergedMessages
  }

  function restoreMessages() {
    // One-time cleanup: drop any legacy cached messages so stale/inconsistent
    // data from before the server-authoritative rewrite can't resurface.
    try {
      localStorage.removeItem('ciao-project-messages')
    } catch { /* ignore */ }
  }

  function persistMessages() {
    // No-op: server (SDK session file) is the source of truth. Keeping the
    // function so existing call sites stay valid during the transition.
  }

  function restoreState() {
    try {
      const ws = localStorage.getItem('ciao-active-workspace')
      if (ws) activeWorkspace.value = ws
      const cid = localStorage.getItem('ciao-active-chat')
      if (cid) activeChatId.value = cid
      const fc = localStorage.getItem('ciao-file-comments')
      if (fc) fileComments.value = JSON.parse(fc)
      const pf = localStorage.getItem('ciao-pinned-files')
      if (pf) pinnedFilePaths.value = JSON.parse(pf)
      const pd = localStorage.getItem('ciao-dismissed-auto-pins')
      if (pd) dismissedAutoPins.value = normalizeDismissedAutoPins(JSON.parse(pd))
      const pi = localStorage.getItem('ciao-pending-images')
      if (pi) pendingImagesByChat.value = normalizePendingBuckets<string>(JSON.parse(pi), activeChatId.value)
      const pc = localStorage.getItem('ciao-pending-comments')
      if (pc) pendingCommentsByChat.value = normalizePendingBuckets<PendingComment>(JSON.parse(pc), activeChatId.value)
      const pcc = localStorage.getItem('ciao-pending-chat-comments')
      if (pcc) pendingChatCommentsByChat.value = normalizePendingBuckets<PendingChatComment>(JSON.parse(pcc), activeChatId.value)
      const ssa = localStorage.getItem('ciao-stream-started-at')
      if (ssa) streamStartedAt.value = JSON.parse(ssa)
      const ua = localStorage.getItem('ciao-unacked-sends')
      if (ua) {
        // Sends that outlived a full page reload (the suspension → refresh
        // path). Recovered when the chat's socket reconnects and history
        // reloads; dropped if the turn meanwhile landed server-side.
        try {
          const parsed: unknown = JSON.parse(ua)
          if (parsed && typeof parsed === 'object') {
            for (const [cid, entry] of Object.entries(parsed as Record<string, unknown>)) {
              const row = entry as Partial<UnackedSend> | null
              if (row && typeof row.text === 'string' && row.text.trim()) {
                unackedSends[cid] = {
                  text: row.text,
                  images: Array.isArray(row.images) ? row.images.map(String) : undefined,
                  at: typeof row.at === 'number' ? row.at : Date.now(),
                  attempts: typeof row.attempts === 'number' ? row.attempts : 0,
                }
              }
            }
          }
        } catch { /* malformed cache; drop it */ }
      }
    } catch { /* ignore */ }
  }

  function persistStreamStartedAt() {
    try {
      localStorage.setItem('ciao-stream-started-at', JSON.stringify(streamStartedAt.value))
    } catch { /* ignore */ }
  }

  function persistFileComments() {
    try {
      localStorage.setItem('ciao-file-comments', JSON.stringify(fileComments.value))
    } catch { /* ignore */ }
  }

  function persistPinnedFiles() {
    try {
      localStorage.setItem('ciao-pinned-files', JSON.stringify(pinnedFilePaths.value))
    } catch { /* ignore */ }
  }

  // Older builds stored `{ [chatId]: true }`, a chat-wide block. Drop those
  // rather than translating them: the flag they encoded ("never surface here
  // again") is the bug this shape replaces, and the file it referred to is not
  // recoverable from it.
  function normalizeDismissedAutoPins(raw: unknown): Record<string, string[]> {
    if (!raw || typeof raw !== 'object') return {}
    const out: Record<string, string[]> = {}
    for (const [id, value] of Object.entries(raw as Record<string, unknown>)) {
      if (!Array.isArray(value)) continue
      const paths = value.filter((p): p is string => typeof p === 'string' && !!p)
      if (paths.length) out[id] = paths
    }
    return out
  }

  function persistDismissedAutoPins() {
    try {
      localStorage.setItem('ciao-dismissed-auto-pins', JSON.stringify(dismissedAutoPins.value))
    } catch { /* ignore */ }
  }

  function persistState() {
    try {
      localStorage.setItem('ciao-active-workspace', activeWorkspace.value)
      if (activeChatId.value) localStorage.setItem('ciao-active-chat', activeChatId.value)
      else localStorage.removeItem('ciao-active-chat')
    } catch { /* ignore */ }
  }

  function persistPendingImages() {
    try {
      localStorage.setItem('ciao-pending-images', JSON.stringify(pendingImagesByChat.value))
    } catch { /* ignore */ }
  }

  function persistPendingComments() {
    try {
      localStorage.setItem('ciao-pending-comments', JSON.stringify(pendingCommentsByChat.value))
    } catch { /* ignore */ }
  }

  function persistPendingChatComments() {
    try {
      localStorage.setItem('ciao-pending-chat-comments', JSON.stringify(pendingChatCommentsByChat.value))
    } catch { /* ignore */ }
  }

  function restoreUnread() {
    try {
      const saved = localStorage.getItem('ciao-unread')
      if (saved) unread.value = JSON.parse(saved)
    } catch { /* ignore */ }
  }

  function persistUnread() {
    try {
      localStorage.setItem('ciao-unread', JSON.stringify(unread.value))
    } catch { /* ignore */ }
  }

  function clearUnread(chatId: string) {
    if (unread.value[chatId]) {
      delete unread.value[chatId]
      persistUnread()
    }
  }

  function postServiceWorkerMessage(message: Record<string, unknown>) {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return
    const controller = navigator.serviceWorker.controller
    if (controller) {
      try { controller.postMessage(message) } catch { /* ignore */ }
      return
    }
    // On a cold iOS standalone launch the worker can be active before it has
    // taken control of the page. Deliver the clear once it is ready instead
    // of leaving the OS notification behind until the next navigation.
    void navigator.serviceWorker.ready
      .then(registration => registration.active?.postMessage(message))
      .catch(() => { /* ignore */ })
  }

  // Server-authoritative unread: a chat is unread if last_activity_at is
  // strictly newer than last_read_at. ISO-8601 timestamps compare correctly
  // as strings. The local `unread` ref is an optimistic overlay used for
  // offline push increments and between WS event and the server round-trip;
  // if set it wins. The getter returns 0 or 1 — the bell dropdown surfaces
  // the list, so an exact per-chat count isn't needed.
  function chatUnread(chatId: string): number {
    const chat = chats.value.find(c => c.chat_id === chatId)
    // Delegate completion is internal model-to-model traffic. It wakes the
    // supervisor, so the child must never create a second unread notification.
    if (chat && isNestedDelegate(chat)) return 0
    // Invariant: the chat the user is actively looking at is, by definition,
    // read. Suppress the badge regardless of the server's last_read_at. This
    // also closes a race in `chat_result_ready` where api.get('/api/chats')
    // can resolve before POST /read is processed and briefly roll back the
    // optimistic last_read_at update.
    if (chatId === activeChatId.value && documentVisible.value) return 0
    if (unread.value[chatId]) return 1
    if (!chat) return 0
    const activity = chat.last_activity_at || ''
    const read = chat.last_read_at || ''
    return activity && activity > read ? 1 : 0
  }

  // A chat blocked on AskUserQuestion or an Approve/Deny prompt — persisted
  // on the chat and mirrored in ephemeral activeQuestions/pendingPermissions
  // while the picker/card is live. Unlike unread, this stays visible even
  // when the chat is the active tab.
  //
  // Also true when a delegate nested under this chat (recursively) is
  // blocked the same way: a supervisor whose subagent is stuck on an
  // Approve/Deny prompt is exactly as blocked as one asked a question
  // directly, and mirroring only chatHasActiveDelegates's "still working"
  // signal left an approval request reading as mere background activity
  // instead of something the user must act on.
  function chatNeedsInput(chatId: string, seen: Set<string> = new Set()): boolean {
    if (activeQuestions.value[chatId]?.length) return true
    if (pendingPermissions.value[chatId]?.length) return true
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (parseQuestions(chat?.pending_question).length > 0) return true
    if (chat?.pending_permission) return true
    if (seen.has(chatId)) return false
    seen.add(chatId)
    return activeDelegatesFor(chatId).some(d => chatNeedsInput(d.chat_id, seen))
  }

  // The first outstanding question is useful on the home card, where it can
  // tell the user what needs an answer before they open the chat.
  function chatPendingQuestion(chatId: string): string | null {
    const chat = chats.value.find(c => c.chat_id === chatId)
    const questions = activeQuestions.value[chatId]?.length
      ? activeQuestions.value[chatId]
      : parseQuestions(chat?.pending_question)
    const question = questions[0]?.question.trim()
    return question || null
  }

  function projectNeedsInput(projectId: string): number {
    return projectChats(projectId).filter(c => chatNeedsInput(c.chat_id)).length
  }

  function projectUnread(projectId: string): number {
    return projectChats(projectId).reduce((sum, c) => sum + chatUnread(c.chat_id), 0)
  }

  function workspaceUnread(ws: WorkspaceName): number {
    return projects.value
      .filter(p => p.workspace === ws)
      .reduce((sum, p) => sum + projectUnread(p.project_id), 0)
  }

  function workspaceNeedsInput(ws: WorkspaceName): number {
    return projects.value
      .filter(p => p.workspace === ws)
      .reduce((sum, p) => sum + projectNeedsInput(p.project_id), 0)
  }

  const totalUnread = computed(() =>
    chats.value.reduce((sum, c) => sum + (c.archived ? 0 : chatUnread(c.chat_id)), 0),
  )

  // One chat can contribute at most one attention item. Keep this aggregate
  // global because the rail is not workspace-scoped; workspace toggles expose
  // the same underlying signals within their selected workspace.
  const attentionChatCount = computed(() =>
    chats.value.reduce(
      (sum, c) => sum + (!c.archived && (chatNeedsInput(c.chat_id) || chatUnread(c.chat_id) > 0) ? 1 : 0),
      0,
    ),
  )

  // Cross-device read: optimistic local clear + POST to server. The server
  // publishes `chat_read` over /ws/events so other devices/tabs update too.
  async function markRead(chatId: string) {
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat) return
    // Optimistic: clear overlay immediately so UI responds without waiting.
    if (unread.value[chatId]) {
      delete unread.value[chatId]
      persistUnread()
    }
    // Also bump the local copy of last_read_at so chatUnread returns 0 right
    // away even before the WS hydration echoes back.
    const nowIso = new Date().toISOString()
    const act = chat.last_activity_at || ''
    if (!chat.last_read_at || chat.last_read_at < act || chat.last_read_at < nowIso) {
      chat.last_read_at = nowIso
    }
    // Ask SW to drop its cache entry for this chat and refresh the native
    // badge. Existing message type kept for compatibility with the SW.
    postServiceWorkerMessage({ type: 'chat-focused', chat_id: chatId })
    try {
      await api.post(`/api/chats/${chatId}/read`, {})
    } catch { /* fire-and-forget; next fetchAll will reconcile */ }
  }

  async function markAllRead() {
    // Optimistic: clear all overlays, bump read timestamps locally.
    unread.value = {}
    persistUnread()
    const nowIso = new Date().toISOString()
    for (const chat of chats.value) {
      if (chat.archived) continue
      const act = chat.last_activity_at || ''
      if (act && act > (chat.last_read_at || '')) {
        chat.last_read_at = nowIso
      }
    }
    postServiceWorkerMessage({ type: 'clear-badge' })
    try {
      await api.post('/api/chats/read-all', {})
    } catch { /* ignore; will reconcile on next fetchAll */ }
  }

  // ── Data fetching ───────────────────────────────────────────────────

  async function fetchAll() {
    try {
      restoreMessages()
      restoreState()
      restoreUnread()
      const [workspaceResponse, p, c] = await Promise.all([
        api.get<WorkspacesResponse>('/api/workspaces'),
        api.get<ProjectInfo[]>('/api/projects'),
        api.get<ChatInfo[]>('/api/chats'),
      ])
      workspaces.value = workspaceResponse.workspaces || []
      workspaceProviderOptions.value = workspaceResponse.provider_options?.length
        ? workspaceResponse.provider_options
        : [{ value: 'claude', label: 'Claude' }]
      projects.value = p
      reconcileChatList(c)
      const knownWorkspaceNames = workspaceOptions.value.map(w => w.name)
      if (!knownWorkspaceNames.includes(activeWorkspace.value)) {
        activeWorkspace.value = workspaceResponse.active || knownWorkspaceNames[0] || 'personal'
      }

      // Initial active-chat resolution:
      //   1) URL /chat/:chatId represents the user's direct intent on a
      //      reload, notification, or deep link.
      //   2) Ordinary launches stay on the home screen. In particular, do not
      //      reopen the chat that happened to be active in the previous run.
      const { router } = await import('../router')
      const urlChatId = (router.currentRoute.value.params.chatId as string | undefined)
        || (typeof window !== 'undefined'
          ? window.location.pathname.match(/^\/chat\/([^/]+)/)?.[1]
          : undefined)
      if (urlChatId && chatExistsInList(urlChatId, c)) {
        await ensureWorkspaceForChat(urlChatId)
        activeChatId.value = urlChatId
      } else if (!bootstrapped.value) {
        // Boot only. fetchAll is also a refresh — SchedulePanel and
        // SchedulesView call it while the app is running — and clearing the
        // selection there dropped the user's open chat just because the
        // current route was /schedules, sending them to the home screen when
        // they navigated back.
        activeChatId.value = null
      }
      persistState()
      if (activeChatId.value) {
        // Only clear unread if the chat is actually on screen. This used to mark
        // read unconditionally, so a fetchAll while the window was hidden (the
        // desktop app launching at login, or a background tab) silently cleared
        // a just-finished chat's unread — losing both the tray badge and the
        // in-app marker. The chat_result_ready handler already gates on
        // visibility for the same reason; match it.
        if (typeof document === 'undefined' || document.visibilityState === 'visible') {
          void markRead(activeChatId.value)
        }
        // Detached on purpose: `bootstrapped` is set when fetchAll resolves and
        // it gates the app shell, so awaiting the active chat's full history
        // here made the whole home page wait on parsing one (possibly very
        // long) transcript. `messages` is reactive, so the chat pane fills in
        // on its own.
        //
        // The two calls stay ordered inside: connecting the socket before the
        // fetch resolves would let an incoming message be clobbered by the
        // fetch result overwriting messages[chatId].
        const bootChatId = activeChatId.value
        void (async () => {
          await loadMessages(bootChatId, { waitForSettledReply: true })
          connectWs(bootChatId)
          requestReentrySummaryIfUseful(bootChatId)
        })()
      }
      // Open the cross-chat awareness socket once per app session.
      connectEventsWs()
      // If a push arrived while the PWA was closed/suspended and
      // notificationclick didn't fire (iOS quirk), the SW still has the
      // target chat cached. Query it and navigate if present.
      checkPendingTarget()
      if (!bootstrapped.value) {
        void checkPackageStatus()
        window.setInterval(checkPackageStatus, UPDATE_CHECK_INTERVAL_MS)
      }
    } finally {
      bootstrapped.value = true
    }
  }

  /**
   * Re-apply local archive intent to a server chat-list payload. A payload that
   * still reports a pending-archive chat as active is stale (its GET raced the
   * archive POST), so the row is kept archived rather than flickering back into
   * the sidebar. Confirmed (or vanished) ids drop out of the pending map.
   */
  function applyPendingArchived(nextChats: ChatInfo[]): ChatInfo[] {
    if (!pendingArchived.value.size) return nextChats
    const present = new Set(nextChats.map(c => c.chat_id))
    for (const id of [...pendingArchived.value]) {
      if (!present.has(id)) pendingArchived.value.delete(id)
    }
    return nextChats.map(c => {
      if (!pendingArchived.value.has(c.chat_id)) return c
      if (c.archived) {
        pendingArchived.value.delete(c.chat_id)
        return c
      }
      return { ...c, archived: true }
    })
  }

  function reconcileChatList(nextChats: ChatInfo[]) {
    chats.value = applyPendingArchived(nextChats)

    // Prune messages for deleted chats.
    const validIds = new Set(nextChats.map(ch => ch.chat_id))

    // Offer back any draft whose chat no longer exists — most likely swept
    // as an abandoned empty chat before its unsent text could be sent (#277).
    // Load-time only (not on every in-session refresh; `bootstrapped` is
    // still false on the very first call of a page load, same flag used
    // above in fetchAll to distinguish boot from refresh), and the original
    // key is left in place until the user restores or dismisses it, so a
    // reload before either happens just re-offers it rather than losing it.
    if (!bootstrapped.value) {
      for (const orphan of readOrphanCandidates(validIds)) {
        const project = projects.value.find(p => p.project_id === orphan.projectId)
        const origin = project ? `${project.workspace}/${project.name}` : 'a deleted project'
        const preview = orphan.text.length > 80 ? `${orphan.text.slice(0, 80)}…` : orphan.text
        pushToast({
          chat_id: '',
          title: 'Recovered an unsent draft',
          body: `From ${origin}: ${preview}`,
          variant: 'error',
          restoreDraft: {
            originalChatId: orphan.chatId,
            projectId: orphan.projectId,
            text: orphan.text,
            workspace: project?.workspace ?? orphan.workspace,
          },
        })
      }
    }
    for (const key of Object.keys(messages.value)) {
      if (!validIds.has(key)) delete messages.value[key]
    }
    for (const key of Object.keys(resolvedQuestions.value)) {
      if (!validIds.has(key)) delete resolvedQuestions.value[key]
    }
    persistMessages()

    // Reconcile overlay: drop entries for deleted chats, and for chats that
    // the server already considers read (stale local flag from e.g. an
    // offline push that was later read on another device).
    const byId = new Map(nextChats.map(ch => [ch.chat_id, ch]))
    for (const key of Object.keys(unread.value)) {
      const chat = byId.get(key)
      if (!chat) {
        delete unread.value[key]
        continue
      }
      const act = chat.last_activity_at || ''
      const read = chat.last_read_at || ''
      if (!act || act <= read) {
        delete unread.value[key]
      }
    }
    persistUnread()
  }

  /**
   * Reconcile against a lightweight `?active_only=1` poll. The server omits
   * archived chats (the archive can be thousands of rows), so archived rows are
   * preserved from what we already hold locally instead of being dropped by a
   * wholesale array replace. Active rows replace their peers in place.
   */
  function reconcileActiveChats(activeChats: ChatInfo[]) {
    const merged: ChatInfo[] = []
    const activeById = new Map(activeChats.map(c => [c.chat_id, c]))
    for (const existing of chats.value) {
      if (existing.archived) {
        // Archived rows are kept as-is; an active-only poll must never
        // resurrect them. Consume any matching payload id so it isn't
        // re-appended below.
        merged.push(existing)
        activeById.delete(existing.chat_id)
      } else if (activeById.has(existing.chat_id)) {
        merged.push(activeById.get(existing.chat_id)!)
        activeById.delete(existing.chat_id)
      }
      // else: an active row that vanished server-side is dropped.
    }
    for (const c of activeById.values()) merged.push(c)
    chats.value = merged
    // Reuse reconcileChatList's side effects (message pruning, unread, overlay).
    reconcileChatList(merged)
  }

  function hasSettledHistory(chatId: string): boolean {
    // Server still streaming this chat — session files already contain
    // mid-turn assistant progress text, which must not look "settled".
    if (projectStreaming.value[chatId]) return false
    const localMessages = messages.value[chatId] || []
    const last = localMessages[localMessages.length - 1]
    if (!last) {
      // Empty history is settled too: some turns end without producing any
      // row (the image-capability pre-flight aborts before dispatch, so no
      // provider session or transcript is ever written). Requiring a trailing
      // assistant/system row here left the spinner running forever on those
      // turns — every caller already gates on !projectStreaming, so an empty
      // transcript with the server idle can only mean the turn is over.
      return true
    }
    if (last.role === 'assistant') return true
    return last.role === 'system' && last.tool_name !== '_activity'
  }

  function clearStreamingState(chatId: string) {
    streaming.value[chatId] = false
    streamingText.value[chatId] = ''
    streamingThinking.value[chatId] = ''
    streamingTimeline.value[chatId] = []
    delete streamingTextPhase.value[chatId]
    delete liveUsage.value[chatId]
    delete streamStartedAt.value[chatId]
    persistStreamStartedAt()
    // Leave `projectStreaming` alone — it is owned by the events websocket
    // (snapshot / chat_streaming_started / done). Clearing it here made
    // mid-turn history polls hide the live Activity.
  }

  async function syncLatest() {
    if (latestSyncInFlight) return
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return
    latestSyncInFlight = true
    try {
      // Active-only: the server skips the (possibly thousands of) archived
      // chats, which this poll never needs — archived rows are held locally.
      // This keeps the every-15s refresh light instead of re-shipping the
      // whole registry to the client on each tick.
      const latestChats = await api.get<ChatInfo[]>('/api/chats?active_only=1')
      // In client mode this request is proxied to the host, so a successful
      // response proves the host is back. The banner was only cleared from a
      // chat WebSocket frame, which never arrives if the socket stays down or
      // no chat is open -- leaving "Can't reach the host" on screen over a
      // working connection until the user reloaded. This poll is the
      // connection-independent recovery signal.
      hostConnectionUnavailable.value = false
      reconcileActiveChats(latestChats)

      const chatId = activeChatId.value
      const chatStillOpen = chatId
        ? latestChats.some(c => c.chat_id === chatId && !c.archived && c.local !== false)
        : false
      if (!chatId || !chatStillOpen) return

      await loadMessages(chatId, { background: true })
      // Only clear a stale local spinner when the server agrees the turn is
      // done. Mid-turn Claude sessions already expose progress assistant
      // text via /messages; treating that as settled promoted those notes
      // into a reply bubble and collapsed Working... into Activity.
      if (
        streaming.value[chatId]
        && !projectStreaming.value[chatId]
        && !queuedMessages.value[chatId]?.length
        && hasSettledHistory(chatId)
      ) {
        clearStreamingState(chatId)
      }
      void loadSubagents(chatId)

      if (typeof WebSocket !== 'undefined') {
        const ws = sockets.value[chatId]
        if (!ws || ws.readyState > WebSocket.OPEN) {
          disconnectWs(chatId)
          connectWs(chatId)
        }
      }
      connectEventsWs()
    } catch {
      // Best-effort refresh. The existing websockets/resume handlers remain
      // the primary live path, and the next interval will try again.
    } finally {
      latestSyncInFlight = false
    }
  }

  // Reconcile the OS app-icon badge with the page's view of truth. The SW
  // increments its own counter on every push but only decrements on
  // notificationclick / chat-focused / clear-badge — so swipe-dismissed
  // notifications, cross-device reads, and PWA-closed reads all leave the
  // SW counter stale.
  //
  // We compute the authoritative per-chat unread map (overlay OR
  // last_activity > last_read) and post it whole to the SW; it replaces
  // its cache and recomputes the OS badge.
  function authoritativeUnreadMap(): Record<string, number> {
    const map: Record<string, number> = {}
    for (const c of chats.value) {
      if (c.archived) continue
      if (chatUnread(c.chat_id) > 0) map[c.chat_id] = 1
    }
    return map
  }
  function postUnreadSync() {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return
    const ctrl = navigator.serviceWorker.controller
    if (!ctrl) return
    try {
      ctrl.postMessage({ type: 'sync-unread', state: authoritativeUnreadMap() })
    } catch { /* ignore */ }
  }
  if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
    // Watch dedupes by JSON content so unrelated chats.value churn doesn't
    // re-fire. Covers in-session changes (mark-read, WS chat_read, push echo,
    // visibility flips that affect the active-chat suppression).
    watch(
      () => JSON.stringify(authoritativeUnreadMap()),
      () => postUnreadSync(),
    )
    // Belt-and-suspenders: when a *new* SW takes control after a deploy,
    // the watch's prior post landed on null/old controller and was lost,
    // and the watch source string didn't change so no re-fire happens.
    // Force a sync on takeover to clear stale OS-level badge counts.
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      postUnreadSync()
    })
    // Also sync once the SW is "ready" (registration + active worker
    // resolved). Handles the cold-boot race where fetchAll completes before
    // controller is ever set.
    void navigator.serviceWorker.ready.then(() => postUnreadSync())
  }

  function selectFirstChat() {
    const wsProjects = workspaceProjects.value
    for (const p of wsProjects) {
      const pChats = projectChats(p.project_id)
      if (pChats.length > 0) {
        activeChatId.value = pChats[0].chat_id
        return
      }
    }
    activeChatId.value = null
  }

  async function transitionToFirstChat() {
    let nextChatId: string | null = null
    const wsProjects = workspaceProjects.value
    for (const p of wsProjects) {
      const pChats = projectChats(p.project_id)
      if (pChats.length > 0) {
        nextChatId = pChats[0].chat_id
        break
      }
    }

    if (nextChatId) {
      activeChatId.value = null
      await switchChat(nextChatId)
    } else {
      activeChatId.value = null
      persistState()
      const { router } = await import('../router')
      router.push('/')
    }
  }

  // ── Workspace actions ────────────────────────────────────────────────
  async function fetchWorkspaces() {
    const res = await api.get<WorkspacesResponse>('/api/workspaces')
    workspaces.value = res.workspaces || []
    workspaceAppDefaultModel.value = res.app_default_model || ''
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
    const names = workspaces.value.map(w => w.name)
    if (activeWorkspace.value && !names.includes(activeWorkspace.value)) {
      activeWorkspace.value = res.active || names[0] || 'personal'
    }
    return res
  }

  async function createWorkspace(payload: Partial<WorkspaceInfo> & { name: string }) {
    const res = await api.post<WorkspacesResponse>('/api/workspaces', payload)
    workspaces.value = res.workspaces || []
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
    return res
  }

  async function updateWorkspace(name: WorkspaceName, payload: Partial<WorkspaceInfo>) {
    const res = await api.patch<WorkspacesResponse>(`/api/workspaces/${encodeURIComponent(name)}`, payload)
    workspaces.value = res.workspaces || []
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
    if (activeWorkspace.value && !workspaces.value.some(w => w.name === activeWorkspace.value)) {
      activeWorkspace.value = res.active || workspaces.value[0]?.name || 'personal'
    }
    return res
  }

  async function deleteWorkspace(name: WorkspaceName) {
    const res = await api.del<WorkspacesResponse>(`/api/workspaces/${encodeURIComponent(name)}`)
    workspaces.value = res.workspaces || []
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
    if (activeWorkspace.value === name) {
      activeWorkspace.value = res.active || workspaces.value[0]?.name || 'personal'
    }
    return res
  }

  // ── Project actions ─────────────────────────────────────────────────

  async function createProject(name: string, context = '') {
    const p = await api.post<ProjectInfo>('/api/projects', {
      name,
      workspace: activeWorkspace.value,
      context,
    })
    // The server broadcasts `project_created` over the WS before returning
    // the HTTP response. If that event lands first, the WS handler has
    // already pushed this project into the list, so skip the duplicate.
    const exists = projects.value.some(x => x.project_id === p.project_id)
    if (!exists) projects.value.push(p)
    return p
  }

  async function updateProject(projectId: string, updates: { name?: string; context?: string }) {
    const p = await api.patch<ProjectInfo>(`/api/projects/${projectId}`, updates)
    const idx = projects.value.findIndex(x => x.project_id === projectId)
    if (idx >= 0) projects.value[idx] = p
    return p
  }

  // Persist a drag-reordered project sequence for the active workspace.
  // Optimistically rewrites local `order` so the sidebar reflects the drop
  // instantly; the server echoes a `projects_reordered` event that reconciles.
  async function reorderProjects(orderedIds: string[]) {
    orderedIds.forEach((pid, index) => {
      const p = projects.value.find(x => x.project_id === pid)
      if (p) p.order = index
    })
    await api.post('/api/projects/reorder', {
      workspace: activeWorkspace.value,
      order: orderedIds,
    })
  }

  // Deliberate delete: drop any draft riding along with these chats now, so
  // a later reload never mistakes it for a sweep-orphaned one.
  function clearDraftsForProject(projectId: string) {
    chats.value.filter(c => c.project_id === projectId).forEach(c => clearChatDraft(c.chat_id))
  }

  async function deleteProject(projectId: string) {
    const activeChatProject = activeChat.value?.project_id
    await api.del(`/api/projects/${projectId}`)
    projects.value = projects.value.filter(p => p.project_id !== projectId)
    clearDraftsForProject(projectId)
    chats.value = chats.value.filter(c => c.project_id !== projectId)
    if (activeChatProject === projectId) {
      if (activeChatId.value) disconnectWs(activeChatId.value)
      await transitionToFirstChat()
    }
  }

  async function completeProject(projectId: string) {
    const activeChatProject = activeChat.value?.project_id
    await api.post(`/api/projects/${projectId}/complete`, {})
    projects.value = projects.value.filter(p => p.project_id !== projectId)
    clearDraftsForProject(projectId)
    chats.value = chats.value.filter(c => c.project_id !== projectId)
    if (activeChatProject === projectId) {
      if (activeChatId.value) disconnectWs(activeChatId.value)
      await transitionToFirstChat()
    }
  }

  // Completed (archived) projects live only as vault folders under
  // projects/completed/; they are not in `projects.value`. Fetched on demand
  // by the sidebar archive modal.
  type CompletedProject = { stem: string; name: string; context: string; workspace: WorkspaceName; vault_doc_path?: string }

  async function fetchCompletedProjects(workspace?: WorkspaceName): Promise<CompletedProject[]> {
    const ws = workspace ?? activeWorkspace.value
    return api.get<CompletedProject[]>(`/api/projects/completed?workspace=${ws}`)
  }

  async function restoreProject(workspace: WorkspaceName, stem: string): Promise<ProjectInfo | null> {
    const res = await api.post<{ ok: boolean; project: ProjectInfo | null }>(
      '/api/projects/completed/restore',
      { workspace, stem },
    )
    // Discovery on the server recreates the project and broadcasts
    // project_created over /ws/events, but adopt the returned project here too
    // so the sidebar updates immediately even if the event races or is missed.
    if (res.project && !projects.value.some(p => p.project_id === res.project!.project_id)) {
      projects.value.push(res.project)
    }
    return res.project
  }

  // ── Chat actions ────────────────────────────────────────────────────

  async function createChat(projectId: string, title = DEFAULT_CHAT_TITLE, seedDraft?: string) {
    // Join an already-in-flight creation for this project instead of firing
    // a second POST: see the comment on pendingChatCreations above.
    const pending = pendingChatCreations[projectId]
    if (pending) return pending

    const promise = (async () => {
      creatingChatProjectIds.value[projectId] = true
      try {
        const c = await api.post<ChatInfo>(`/api/projects/${projectId}/chats`, { title })
        // The server also broadcasts chat_created for this same chat. The
        // broadcast can arrive before the POST response, so reconcile through
        // the ID-aware helper instead of pushing a possible duplicate.
        replaceChat(c)
        messages.value[c.chat_id] = []
        // Write before switching: ChatPanel reads the draft once at mount, so
        // this must already be in storage before the new panel mounts below.
        if (seedDraft) {
          const seedWorkspace = projects.value.find(p => p.project_id === projectId)?.workspace
            ?? activeWorkspace.value
          writeChatDraft(c.chat_id, seedDraft, undefined, { projectId, workspace: seedWorkspace })
        }
        // We just created it, so there is no history to fetch.
        switchChat(c.chat_id, { skipHistory: true })
        return c
      } finally {
        delete creatingChatProjectIds.value[projectId]
        delete pendingChatCreations[projectId]
      }
    })()
    pendingChatCreations[projectId] = promise
    return promise
  }

  async function renameChat(chatId: string, title: string) {
    const c = await api.patch<ChatInfo>(`/api/chats/${chatId}`, { title })
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
  }

  async function updateChat(
    chatId: string,
    updates: {
      model?: string
      mode?: string
      provider?: RuntimeProvider
      thinking_level?: string
    },
  ) {
    const c = await api.patch<ChatInfo>(`/api/chats/${chatId}`, updates)
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
  }

  async function handoverChat(
    chatId: string,
    updates: { model: string; provider: RuntimeProvider },
  ) {
    const visibleMessages = normalizeMessages(messages.value[chatId] || [])
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/handover`, {
      ...updates,
      messages: visibleMessages,
    })
    replaceChat(c)
    await loadMessages(chatId)
    if (activeChatId.value === chatId) {
      disconnectWs(chatId)
      connectWs(chatId)
    }
    return c
  }

  async function forkChat(
    chatId: string,
    copiedMessages: ChatMessage[],
    turnIndex: number,
  ) {
    const snapshot = normalizeMessages(copiedMessages)
    const fork = await api.post<ChatInfo>(`/api/chats/${chatId}/fork`, {
      messages: snapshot,
      turn_index: turnIndex,
    })
    replaceChat(fork)
    messages.value[fork.chat_id] = snapshot
    persistMessages()
    await switchChat(fork.chat_id)
    return fork
  }

  async function moveChat(chatId: string, targetProjectId: string) {
    // Server validates same-workspace + non-archived + project exists.
    // The chat_moved broadcast on /ws/events also reconciles other tabs.
    const c = await api.patch<ChatInfo>(`/api/chats/${chatId}`, { project_id: targetProjectId })
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
    return c
  }

  async function deleteChat(
    chatId: string,
    options?: { selectNext?: boolean; onlyIfEmpty?: boolean },
  ): Promise<boolean> {
    disconnectWs(chatId)
    // `only_if_empty` makes the server apply its own `_is_empty_chat` rule and
    // decline otherwise, so closing a draft can never delete a real chat.
    const query = options?.onlyIfEmpty ? '?only_if_empty=1' : ''
    const result = await api.del<{ deleted?: boolean }>(`/api/chats/${chatId}${query}`)
    if (options?.onlyIfEmpty && result?.deleted === false) return false
    // Deliberate delete: the chat is really gone, so any draft riding along
    // with it is by construction never a sweep casualty — clear it now.
    clearChatDraft(chatId)
    chats.value = chats.value.filter(c => c.chat_id !== chatId)
    delete messages.value[chatId]
    delete reentrySummaries.value[chatId]
    delete reentrySummaryRevisions.value[chatId]
    reentrySummaryRequests.delete(chatId)
    persistMessages()
    if (options?.selectNext !== false && activeChatId.value === chatId) {
      await transitionToFirstChat()
    }
    return true
  }

  // Mirrors the server's `_is_empty_chat` (project_chats.py). It must not be
  // more eager than the server: this deletes the chat outright, and the two
  // rules disagreeing means deleting something the server would have kept.
  //
  // The client cannot see `user_turn_count`, so it substitutes the loaded
  // messages — which is only sound when they are actually loaded. `messages`
  // is undefined for a chat that was never opened, and reading that as "no
  // user turns" made a real conversation look like a discarded draft.
  function isEmptyDraft(chatId: string): boolean {
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat || chat.archived || chat.session_id) return false
    // A renamed chat is a deliberate act, not an abandoned draft.
    if (chat.title !== DEFAULT_CHAT_TITLE) return false
    // So is a typed-but-unsent prompt. The composer persists one per chat
    // (lib/chatDrafts) and Esc closes the chat *while the composer is
    // focused*, so without this, typing a long prompt into a New Chat and
    // hitting Esc deleted it with no way back. Checked locally as well as
    if (readChatDraft(chatId).trim()) return false
    // Staged attachments are unsent content just as much as typed text, and
    // the server cannot see them either: it would agree the chat is empty and
    // delete the pasted screenshot with it.
    if (getPendingBucket(pendingImagesByChat.value, chatId).length) return false
    if (getPendingBucket(pendingCommentsByChat.value, chatId).length) return false
    if (getPendingBucket(pendingChatCommentsByChat.value, chatId).length) return false
    const loaded = messages.value[chatId]
    if (!loaded) return false
    return !loaded.some(message => message.role === 'user')
  }

  async function closeChat(chatId = activeChatId.value): Promise<void> {
    if (!chatId) return
    const emptyDraft = isEmptyDraft(chatId)
    const wasActive = activeChatId.value === chatId
    if (emptyDraft) {
      // A never-used New Chat is only a draft. Delete it on close and leave
      // the home screen empty instead of jumping to another conversation.
      // Clear the view before awaiting the DELETE so the close gesture feels
      // immediate even if the local server is briefly slow.
      if (wasActive) {
        activeChatId.value = null
        persistState()
      }
      // onlyIfEmpty: the server re-checks with the full rule and declines if
      // this is not actually a discardable draft. Closing a chat must never
      // be able to destroy one.
      try {
        await deleteChat(chatId, { selectNext: false, onlyIfEmpty: true })
      } finally {
        // The view is already cleared. A failed DELETE must not also strand
        // the router on /chat/<id> with no active chat behind it.
        await leaveChatView(wasActive)
      }
      return
    }
    // Warm the persistent per-chat summary cache while the user is away.
    // The request is intentionally detached so closing the chat stays
    // immediate; switchChat still requests it as a fallback if this call
    // has not finished by the time the user returns.
    void requestReentrySummary(chatId)
    disconnectWs(chatId)
    await leaveChatView(wasActive)
  }

  async function leaveChatView(wasActive: boolean): Promise<void> {
    if (!wasActive) return
    activeChatId.value = null
    persistState()
    const { router } = await import('../router')
    await router.push('/')
  }

  function clearReentrySummary(chatId: string): void {
    delete reentrySummaries.value[chatId]
    reentrySummaryRevisions.value[chatId] = (reentrySummaryRevisions.value[chatId] || 0) + 1
  }

  async function requestReentrySummary(chatId: string): Promise<void> {
    if (reentrySummaryRequests.has(chatId)) return
    reentrySummaryRequests.add(chatId)
    const revision = reentrySummaryRevisions.value[chatId] || 0
    try {
      const result = await api.post<{ summary?: string }>(`/api/chats/${chatId}/reentry-summary`, {})
      const summary = typeof result?.summary === 'string' ? result.summary.trim() : ''
      if (
        summary
        && revision === (reentrySummaryRevisions.value[chatId] || 0)
        && chats.value.some(chat => chat.chat_id === chatId)
      ) {
        reentrySummaries.value[chatId] = summary
      }
    } catch {
      // Apple Intelligence is optional. A failed/unavailable summary should
      // never interfere with opening or using the chat.
    } finally {
      reentrySummaryRequests.delete(chatId)
    }
  }

  function requestReentrySummaryIfUseful(chatId: string): void {
    if (!readReentrySummaryEnabled()) return
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat || chat.archived) return
    // session_id covers chats whose history is still being hydrated; the
    // message check covers providers/fixtures that do not expose one.
    const hasHistory = Boolean(chat.session_id) || (messages.value[chatId] || []).some(
      message => message.role === 'user' || message.role === 'assistant',
    )
    if (hasHistory && !reentrySummaries.value[chatId]) {
      void requestReentrySummary(chatId)
    }
  }

  // Toggling the preference off also evicts any cached summaries so the
  // bubble disappears immediately rather than lingering for the rest of
  // the session. Toggling on does nothing — the next chat open will fetch
  // its own summary, no warm-up needed.
  function setReentrySummaryEnabled(enabled: boolean): void {
    if (!enabled) {
      for (const chatId of Object.keys(reentrySummaries.value)) {
        clearReentrySummary(chatId)
      }
    }
  }

  async function archiveChat(chatId: string) {
    // Guard against double-clicks: the button is already disabled while the
    // optimistic state is live, but event handlers can still double-fire.
    if (archivingChats.value[chatId]) return
    // The server cascades archival to delegate subchats. Note this filter has
    // no `local !== false` guard, unlike activeDelegatesFor(): the cascade
    // covers remote delegates too, so the socket bookkeeping here has to as
    // well. Only user-facing counts exclude them.
    const childIds = chats.value
      .filter(c => c.spawned_from_chat_id === chatId && !c.archived)
      .map(c => c.chat_id)
    const allIds = [chatId, ...childIds]
    // Remember which sockets we actually closed. If the POST fails these chats
    // are all still live, and a closed socket is marked as an intentional close
    // so nothing auto-reconnects it — the chat would go silent, with no tokens,
    // permission cards or AskUserQuestion prompts, and no sign anything broke.
    const closedIds = allIds.filter(id => Boolean(sockets.value[id]))
    // Snapshot for rollback if the POST fails. A failure must not leave the
    // chat hidden from the sidebar with archived:true and no transcript.
    const prevArchived = new Map<string, boolean>()
    for (const id of allIds) {
      const c = chats.value.find(ch => ch.chat_id === id)
      if (c) prevArchived.set(id, c.archived)
    }
    const wasActive = activeChatId.value === chatId
    // Optimistic UI: hide from active lists immediately and show in the
    // home "archiving…" queue so the panel can close without waiting for
    // the server's disk work (transcript write + delegate cascade).
    for (const id of allIds) {
      archivingChats.value[id] = true
      pendingArchived.value.add(id)
      const c = chats.value.find(ch => ch.chat_id === id)
      if (c) c.archived = true
    }
    if (wasActive) {
      activeChatId.value = null
      persistState()
      // Keep the URL in sync with the optimistically closed pane. Fire-and-
      // forget so the POST is not blocked on a router import.
      import('../router').then(({ router }) => {
        if (router.currentRoute.value.params.chatId === chatId) router.push('/')
      }).catch(() => {})
    }
    disconnectWs(chatId)
    for (const childId of childIds) disconnectWs(childId)

    let res: ArchiveChatResponse
    try {
      res = await api.post<ArchiveChatResponse>(`/api/chats/${chatId}/archive`)
    } catch (e) {
      // Roll back optimistic mutation: chat reappears in the sidebar / home
      // active lanes and its socket is put back so streaming resumes.
      for (const [id, prev] of prevArchived) {
        const c = chats.value.find(ch => ch.chat_id === id)
        if (c) c.archived = prev
        delete archivingChats.value[id]
        pendingArchived.value.delete(id)
      }
      for (const id of allIds) {
        delete archivingChats.value[id]
        pendingArchived.value.delete(id)
      }
      for (const id of closedIds) connectWs(id)
      if (wasActive && activeChatId.value === null) {
        activeChatId.value = chatId
        persistState()
        import('../router').then(({ router }) => {
          if (!router.currentRoute.value.params.chatId) router.push(`/chat/${chatId}`)
        }).catch(() => {})
      }
      pushErrorToast('Could not archive chat', `${errorMessage(e)}`)
      throw e
    }

    // Mark only what the server confirms. Flipping every child on a bare 2xx
    // dropped skipped delegates out of the sidebar, recentChats and
    // activeChatsAll while they were still streaming, and listed them in the
    // archive with no transcript behind them. Optimistic already flipped them,
    // so revert any id the server did not confirm.
    const confirmed = new Set(
      Array.isArray(res?.archived_chat_ids) ? res.archived_chat_ids : [chatId],
    )
    for (const id of allIds) {
      if (!confirmed.has(id)) {
        const c = chats.value.find(ch => ch.chat_id === id)
        if (c) c.archived = prevArchived.get(id) ?? false
        pendingArchived.value.delete(id)
      } else if (id === chatId && res?.postprocess) {
        const c = chats.value.find(ch => ch.chat_id === id)
        if (c) {
          // The response closes the race where the chat_postprocess event is
          // emitted after the archive request has already cleared this pane.
          c.postprocess = res.postprocess
        }
      }
      delete archivingChats.value[id]
    }
    // Any child the entry never listed stays removed from archiving map
    // (it was not an optimistic id, but guard anyway).
    for (const chat of chats.value) {
      if (confirmed.has(chat.chat_id) && chat.chat_id !== chatId && res?.postprocess) {
        // Delegates' postprocess arrives via /ws/events, not the response.
      }
    }
    // A child the server did not archive is still running: put its socket back.
    for (const id of closedIds) {
      if (!confirmed.has(id)) connectWs(id)
    }

    // Archiving is immediate, so it may have discarded a delegate's in-flight
    // turn. Say so — it is the user's work that was thrown away.
    const stopped = (res?.stopped_chat_ids || []).filter(id => id !== chatId)
    if (stopped.length) {
      pushToast({ chat_id: '', ...archiveStoppedToast(stopped.length) })
    }
    const failed = res?.failed_chat_ids || []
    if (failed.length) {
      const toast = archiveFailedToast(failed.length)
      pushErrorToast(toast.title, toast.body)
    }
    // Active already cleared optimistically; keep the guard for races where
    // the user switched chats between the optimistic clear and the response.
    if (activeChatId.value === chatId) {
      activeChatId.value = null
      persistState()
    }
  }

  async function continueArchivedChat(chatId: string) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/continue`)
    chats.value.push(c)
    messages.value[c.chat_id] = []
    switchChat(c.chat_id)
    return c
  }

  async function setChatRetry(chatId: string, prompt: string, images?: string[]) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/retry`, {
      action: 'set',
      prompt,
      images: images || [],
    })
    replaceChat(c)
    return c
  }

  async function stopChatRetry(chatId: string) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/retry`, { action: 'stop' })
    replaceChat(c)
    return c
  }

  async function tryChatRetryNow(chatId: string) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/retry`, { action: 'try_now' })
    replaceChat(c)
    // If this tab is already on the chat, reconnect so the per-chat WS
    // attaches to the new broker stream started by the HTTP action.
    if (activeChatId.value === chatId) {
      disconnectWs(chatId)
      connectWs(chatId)
    }
    return c
  }

  /** Re-run session-insights extraction for one archived chat (text-mode). */
  async function retryInsights(chatId: string): Promise<void> {
    const res = await api.post<{ status: string }>(`/api/chats/${chatId}/retry-insights`)
    const status = res?.status
    if (status === 'already_has') {
      pushToast({ chat_id: '', title: 'Insights already added', body: 'This chat already has a Session insights section.' })
    } else if (status === 'running') {
      pushToast({ chat_id: '', title: 'Already tidying', body: 'This chat is already being processed.' })
    }
  }

  function replaceChat(chat: ChatInfo) {
    const idx = chats.value.findIndex(x => x.chat_id === chat.chat_id)
    if (idx >= 0) chats.value[idx] = chat
    else chats.value.push(chat)
  }

  async function newSession(chatId: string) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/new`)
    // A reset clears `archived` server-side on the same chat_id, so any stale
    // local archive intent for it must go with it.
    pendingArchived.value.delete(chatId)
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
    messages.value[chatId] = []
    persistMessages()
    // Reconnect WebSocket for fresh session
    disconnectWs(chatId)
    connectWs(chatId)
  }

  // ── Message loading from server ──────────────────────────────────────

  /**
   * `background: true` refreshes history without claiming the loading flag.
   * The 15s poll and the socket watchdog both re-read every open chat, and on
   * a chat with nothing to render yet (a brand-new one) the flag paints the
   * full-size "Loading conversation" skeleton — so an idle empty chat blinked
   * through that card on every tick. A user-initiated open still shows it.
   *
   * `waitForSettledReply: true` (switchChat's own open, not background polls)
   * keeps that same loading flag held past the first fetch when the chat's
   * last turn is an unanswered user message: the history endpoint can resolve
   * before the SDK session file catches up with the just-finished reply (e.g.
   * opening a chat right as its turn settles, or from a push notification),
   * which used to clear the loading flag and show an incomplete transcript
   * with no visible sign anything was still pending. Retries on the same
   * cadence as reconcileAfterResult until the reply lands, streaming visibly
   * takes over, or the budget runs out. A chat with no messages, or one
   * already ending in a settled reply, skips this — every open must not pay
   * for a wait nothing is actually pending.
   */
  async function loadMessages(
    chatId: string,
    opts?: { background?: boolean; waitForSettledReply?: boolean },
  ) {
    const generation = (messageLoadGenerations.get(chatId) || 0) + 1
    messageLoadGenerations.set(chatId, generation)
    if (!opts?.background) loadingMessages.value[chatId] = true
    try {
      await loadMessagesFromServer(chatId)
      if (opts?.waitForSettledReply && !opts?.background) {
        const last = (messages.value[chatId] || []).at(-1)
        const awaitingReply = last?.role === 'user'
          && !streaming.value[chatId]
          && !projectStreaming.value[chatId]
        if (awaitingReply) {
          for (const delay of [300, 700, 1500, 3000]) {
            if (messageLoadGenerations.get(chatId) !== generation) return
            await new Promise(r => setTimeout(r, delay))
            await loadMessagesFromServer(chatId)
            if (hasSettledHistory(chatId) || streaming.value[chatId] || projectStreaming.value[chatId]) break
          }
        }
      }
    } finally {
      // A refresh can overlap a chat switch or a reconnect. Only the newest
      // request owns the loading flag, otherwise an older response can hide
      // the indicator while the current history is still pending.
      if (messageLoadGenerations.get(chatId) === generation) {
        delete loadingMessages.value[chatId]
        messageLoadGenerations.delete(chatId)
      }
    }
  }

  type ServerRow = { role: string; content: string; tool_name?: string; images?: string[]; turn_index?: number; sent_at?: string; duration_ms?: number; is_error?: boolean; file_path?: string; action?: string; tool?: string; phase?: 'commentary' | 'final_answer'; i?: number; lazy?: boolean; full_length?: number; unattended?: boolean }
  const toChatMessage = (m: ServerRow) => ({
    role: m.role as 'user' | 'assistant' | 'system',
    content: m.content,
    // sent_at is the persisted send-time (user) or completion-time
    // (assistant) recorded at the orchestration layer. Empty string for
    // pre-feature chats — the renderer treats it as "no time".
    timestamp: m.sent_at || '',
    tool_name: m.tool_name,
    images: m.images,
    // Preserve server-assigned turn_index so user_echo replays (from WS
    // reconnect mid-turn or right after) can dedup against hydrated
    // history. Dropping this caused duplicate user bubbles: the dedup at
    // the user_echo handler matches by turn_index first, and when every
    // hydrated bubble has turn_index: undefined, the replayed echo falls
    // through to msgs.push and renders a second copy of the same turn.
    turn_index: m.turn_index,
    duration_ms: m.duration_ms,
    is_error: m.is_error,
    // Loop/schedule tick marker (↻). The backend records it per turn at
    // send time; without mapping it here a reload made automated turns
    // read as user-authored.
    unattended: m.unattended || undefined,
    // _filecard fields. Empty/undefined for non-file rows.
    file_path: m.file_path,
    action: m.action,
    tool: m.tool,
    phase: m.phase,
    // Envelope annotations (absolute index + lazy marker). Undefined on
    // legacy flat responses.
    i: m.i,
    lazy: m.lazy,
    full_length: m.full_length,
  })

  async function loadMessagesFromServer(chatId: string) {
    // Restore the AskUserQuestion picker before touching history. Runs on every
    // chat open / reconnect, so a reloaded chat paused on a question shows the
    // interactive picker again instead of the dead trace row. Independent of
    // server history, so it survives the early returns below.
    rebuildPendingQuestion(chatId)
    rebuildPendingPermission(chatId)
    // Fetch authoritative history from the SDK session on the server.
    // This catches schedule outputs, turns from other devices, etc.
    // `limit=50` asks for the paginated envelope (newest tail window); an
    // older server answers with the legacy flat array and the code below
    // handles both transparently.
    type ServerEnvelope = { items: ServerRow[]; total: number; offset: number; limit: number; hasMore: boolean; nextOffset: number | null }
    try {
      const serverMsgs = await api.get<ServerRow[] | ServerEnvelope>(
        `/api/chats/${chatId}/messages?limit=50`
      )
      if (Array.isArray(serverMsgs) && !serverMsgs.length) {
        reconcileQueuedWithMessages(chatId)
        return
      }

      // ── Envelope mode: merge the newest window into the cached timeline ──
      if (!Array.isArray(serverMsgs)) {
        const env = serverMsgs
        historyMeta.value[chatId] = {
          total: env.total,
          hasMore: Boolean(env.hasMore),
          nextOffset: env.nextOffset ?? null,
          limit: env.limit || 50,
        }

        let windowRows = normalizeMessages(env.items.map(toChatMessage))

        // Mid-stream guard, window-scoped version of the legacy rule below:
        // while the chat streams, the live trace owns the in-flight turn, so
        // drop trailing history past the last known user bubble unless the
        // window already ends in a settled reply.
        if (projectStreaming.value[chatId] && windowRows.length) {
          const lastServer = windowRows[windowRows.length - 1]
          const serverTurnSettled = Boolean(
            lastServer
            && lastServer.role === 'assistant'
            && !lastServer.is_error
            && lastServer.timestamp,
          )
          if (!serverTurnSettled) {
            const localMsgs = messages.value[chatId] || []
            let lastLocalUserIdx = -1
            for (let i = localMsgs.length - 1; i >= 0; i--) {
              if (localMsgs[i].role === 'user') {
                lastLocalUserIdx = i
                break
              }
            }
            if (lastLocalUserIdx >= 0) {
              const lastLocalUser = localMsgs[lastLocalUserIdx]
              let serverLastUserIdx = -1
              for (let i = windowRows.length - 1; i >= 0; i--) {
                if (windowRows[i].role === 'user' && windowRows[i].content === lastLocalUser.content) {
                  serverLastUserIdx = i
                  break
                }
              }
              if (serverLastUserIdx >= 0) {
                windowRows = windowRows.slice(0, serverLastUserIdx + 1)
              }
            }
          }
        }

        const local = messages.value[chatId] || []
        const firstIndex = local.length ? local[0].i : undefined
        // How far the cache claims to reach, from its last INDEXED row: rows
        // added locally (a failed-send notice, say) carry no index and must not
        // inflate it, or a shrink check built on length would discard them.
        let cachedEnd = 0
        for (const row of local) {
          if (typeof row.i === 'number' && row.i + 1 > cachedEnd) cachedEnd = row.i + 1
        }
        if (
          !local.length ||
          typeof firstIndex !== 'number' ||
          env.total <= firstIndex ||
          // The server assembled FEWER rows than we hold - a pruned or
          // unreadable session segment. Merging by index would refresh the
          // prefix and leave the stale tail untouched, showing messages that
          // are no longer part of the chat, so the window wins outright.
          env.total < cachedEnd
        ) {
          // Empty cache, cache from a pre-envelope server, or the session
          // reset/shrank: adopt the window wholesale.
          messages.value[chatId] = windowRows
        } else {
          // Index-addressed merge: refresh rows we already hold, append new
          // tail rows, keep older pages loaded via loadOlderMessages.
          //
          // Rows are looked up by their ABSOLUTE index, never by `abs -
          // firstIndex`: `local` is not a contiguous run of server-indexed
          // rows. An optimistic user bubble, flushed streaming rows and the
          // failed-send notice pushed by recoverUnackedSend all sit in it with
          // `i === undefined`, and every one of them shifted the position
          // arithmetic by one - so the window's rows landed on the wrong slots,
          // rendering an assistant reply twice and silently overwriting the
          // failed-send warning on the next refresh.
          const posByIndex = new Map<number, number>()
          local.forEach((row, pos) => {
            if (typeof row.i === 'number') posByIndex.set(row.i, pos)
          })
          const merged = local.slice()
          // Where the un-indexed live tail begins: everything the client
          // rendered from streaming events (optimistic user bubble, activity
          // groups, the final answer) sits after the last server-indexed row.
          let tailStart = merged.length
          for (let p = merged.length - 1; p >= 0; p--) {
            if (typeof merged[p].i === 'number') {
              tailStart = p + 1
              break
            }
          }
          // Exact identity, except for wire-pruned lazy rows: the server
          // elides an oversized _thinking row's middle, so its content can
          // never equal the live copy that holds the full text. Match those
          // on head + tail + minimum length instead.
          const LAZY_MARKER_RE = /\n… \(\d+ chars hidden, expand to load\)\n/
          const sameRow = (row: ChatMessage, item: ChatMessage) => {
            if (row.role !== item.role) return false
            if ((row.tool_name || '') !== (item.tool_name || '')) return false
            if (row.content === item.content) return true
            if (item.lazy && item.full_length != null) {
              const m = item.content.match(LAZY_MARKER_RE)
              if (m && m.index !== undefined) {
                const head = item.content.slice(0, m.index)
                const tail = item.content.slice(m.index + m[0].length)
                return row.content.length >= item.full_length
                  && row.content.startsWith(head)
                  && row.content.endsWith(tail)
              }
            }
            return false
          }
          for (const item of windowRows) {
            const abs = item.i
            if (typeof abs !== 'number') continue
            const pos = posByIndex.get(abs)
            if (pos !== undefined) {
              merged[pos] = item
              continue
            }
            if (abs < cachedEnd) {
              // An index below cachedEnd that we don't hold is a hole in the
              // cache (loadOlderMessages fills those); skip it rather than
              // appending it out of order at the tail.
              continue
            }
            // A server row the cache holds only as an un-indexed live copy
            // (optimistic user bubble, streamed activity group or final
            // answer) must REPLACE that copy, not land next to it. A refresh
            // while the turn was live (WS reconnect, chat switch back, the
            // post-result reconcile) otherwise appended the server copy of
            // the whole turn — the reported "double message", on the user
            // bubble first and then on the Activity group + answer. Scan the
            // live tail in order so server rows pair with their own turn's
            // copies; identical texts pair one-to-one, so a genuine repeat
            // send keeps both copies countable.
            let reconciled = false
            for (let p = tailStart; p < merged.length; p++) {
              const row = merged[p]
              if (typeof row.i === 'number') continue
              if (!sameRow(row, item)) continue
              // The live copy is the richer one for streamed turns (usage,
              // phase, duration); the server row contributes only its index.
              // A user bubble is the exception: the server owns the canonical
              // turn_index/sent_at, so merge onto the server row.
              merged[p] = item.role === 'user'
                ? mergeMessageFields(item, row)
                : { ...row, i: item.i }
              posByIndex.set(abs, p)
              reconciled = true
              break
            }
            if (reconciled) continue
            // Beyond the cached extent: genuinely new tail rows, appended in
            // the window's own ascending order (after any un-indexed local
            // rows, which are older than anything arriving now).
            posByIndex.set(abs, merged.length)
            merged.push(item)
          }
          messages.value[chatId] = merged
        }
        persistMessages()
        if (streaming.value[chatId]
          && !projectStreaming.value[chatId]
          && !queuedMessages.value[chatId]?.length
        ) {
          const last = messages.value[chatId]?.at(-1)
          if (last && ((last.role === 'assistant' && !last.is_error) || (last.role === 'system' && last.tool_name !== '_activity'))) {
            clearStreamingState(chatId)
          }
        }
        reconcileQueuedWithMessages(chatId)
        return
      }

      let normalizedServer = normalizeMessages(serverMsgs.map(toChatMessage))

      // While the server declares this chat is actively streaming, don't let
      // /messages pull in the assistant's progress into the historical timeline:
      // the live trace already owns the current turn. Loading mid-turn activity
      // creates a duplicate Activity row below the live one.
      //
      // But `projectStreaming` reflects the events-WS view, which can lag the
      // server truth — for example when the chat is opened from a push
      // notification before the events socket re-snapshots `chat_streaming_done`.
      // When the server's history already ends in a *completed* assistant reply
      // (it carries a completion `sent_at`/timestamp overlaid by the
      // orchestration layer), the turn is settled server-side and truncating
      // would silently drop the real answer, leaving only the user's last turn.
      // In that case skip the truncation so the finished reply renders.
      if (projectStreaming.value[chatId]) {
        const lastServer = normalizedServer[normalizedServer.length - 1]
        const serverTurnSettled = Boolean(
          lastServer &&
          lastServer.role === 'assistant' &&
          !lastServer.is_error &&
          lastServer.timestamp,
        )
        if (!serverTurnSettled) {
          const localMsgs = messages.value[chatId] || []
          let lastLocalUserIdx = -1
          for (let i = localMsgs.length - 1; i >= 0; i--) {
            if (localMsgs[i].role === 'user') {
              lastLocalUserIdx = i
              break
            }
          }
          if (lastLocalUserIdx >= 0) {
            const lastLocalUser = localMsgs[lastLocalUserIdx]
            let serverLastUserIdx = -1
            for (let i = normalizedServer.length - 1; i >= 0; i--) {
              if (normalizedServer[i].role === 'user' && normalizedServer[i].content === lastLocalUser.content) {
                serverLastUserIdx = i
                break
              }
            }
            if (serverLastUserIdx >= 0) {
              normalizedServer = normalizedServer.slice(0, serverLastUserIdx + 1)
            }
          }
        }
      }

      let normalizedLocal = normalizeMessages(messages.value[chatId] || [])
      const historyChanged = historySignature(normalizedServer) !== historySignature(normalizedLocal)

      // Heal orphaned optimistic user bubbles. A send queued behind a still
      // streaming turn can leave a turn_index-less copy that the live echo
      // failed to reconcile (see the user_echo handler). The SDK session is
      // authoritative and holds each turn exactly once, so drop any local
      // null-turn_index user bubble whose text already appears as a server
      // user turn before comparing lengths — otherwise the "never shrink
      // history" guard below would preserve the duplicate forever.
      const serverUserContent = new Set(
        normalizedServer.filter(m => m.role === 'user').map(m => m.content),
      )
      if (serverUserContent.size) {
        const pruned = normalizedLocal.filter(
          m => !(m.role === 'user' && m.turn_index == null && serverUserContent.has(m.content)),
        )
        if (pruned.length !== normalizedLocal.length) normalizedLocal = pruned
      }

      if (historySignature(normalizedServer) !== historySignature(normalizedLocal)) {
        // Guard: never replace a longer local history with a shorter server
        // history. This can happen when the SDK session was reset (e.g. resume
        // failure caused a fresh session) and the new session file has fewer
        // messages than the frontend accumulated from streaming events.
        const serverUserCount = normalizedServer.filter(m => m.role === 'user').length
        const localUserCount = normalizedLocal.filter(m => m.role === 'user').length
        if (serverUserCount < localUserCount) {
          const serverUsers = normalizedServer.filter(m => m.role === 'user')
          const localUsers = normalizedLocal.filter(m => m.role === 'user')
          let isPrefix = true
          for (let i = 0; i < serverUsers.length; i++) {
            if (serverUsers[i].content !== localUsers[i].content) {
              isPrefix = false
              break
            }
          }
          const extraLocalUsers = localUsers.slice(serverUsers.length)
          const allExtraAreOptimistic = extraLocalUsers.every(m => m.turn_index == null)
          if (!isPrefix || !allExtraAreOptimistic) {
            console.warn(
              `[loadMessages] Server returned ${serverUserCount} user turns but local has ${localUserCount}; keeping local to avoid data loss`,
            )
            return
          }
        }
        messages.value[chatId] = mergeMetadata(normalizedServer, normalizedLocal)
        persistMessages()
      } else if (historySignature(normalizedLocal) !== historySignature(messages.value[chatId] || [])) {
        messages.value[chatId] = normalizedLocal
        persistMessages()
      }
      if (historyChanged) clearReentrySummary(chatId)
      if (
        streaming.value[chatId]
        && !projectStreaming.value[chatId]
        && !queuedMessages.value[chatId]?.length
        && hasSettledHistory(chatId)
      ) {
        clearStreamingState(chatId)
      }
      reconcileQueuedWithMessages(chatId)
    } catch {
      // Server may not have history yet, that's fine
    }
  }

  // ── Paginated history: older pages + lazy part expansion ────────────

  function canLoadOlder(chatId: string): boolean {
    return Boolean(historyMeta.value[chatId]?.hasMore)
  }

  function isLoadingOlder(chatId: string): boolean {
    return Boolean(loadingOlder.value[chatId])
  }

  /** Fetch the previous page above the loaded window and prepend it. The
   * caller (ChatPanel) preserves scroll position across the prepend. */
  async function loadOlderMessages(chatId: string): Promise<void> {
    const meta = historyMeta.value[chatId]
    if (!meta?.hasMore || meta.nextOffset == null || loadingOlder.value[chatId]) return
    loadingOlder.value[chatId] = true
    type OlderPage = { items: Parameters<typeof toChatMessage>[0][]; total: number; limit: number; hasMore: boolean; nextOffset: number | null }
    try {
      const local = messages.value[chatId] || []
      const firstIndex = local.length ? local[0].i : undefined
      const fetchPage = (offset: number) => api.get<OlderPage>(
        `/api/chats/${chatId}/messages?offset=${offset}&limit=${meta.limit}`
      )
      // `offset` counts BACKWARD from the server's CURRENT total, so
      // `meta.nextOffset` — computed when the tail window was loaded — aims at
      // the wrong boundary as soon as the session grows in between. A 75-row
      // history loaded as indices 25-74 asks for offset 50; once two more rows
      // exist that same offset answers with 0-26, the continuity check below
      // rejects the overlap, and the rejected page's `hasMore: false` used to
      // be persisted — freezing pagination so the oldest messages became
      // permanently unreachable. Address the page by the index we actually
      // need (`total - firstIndex`) and, when the response reveals a newer
      // total, retry once against that index-stable boundary.
      let requestedTotal = meta.total
      let offset = typeof firstIndex === 'number'
        ? Math.max(0, meta.total - firstIndex)
        : meta.nextOffset
      let env = await fetchPage(offset)
      if (Array.isArray(env)) return
      if (typeof firstIndex === 'number' && env.total !== requestedTotal) {
        const corrected = Math.max(0, env.total - firstIndex)
        if (corrected !== offset) {
          requestedTotal = env.total
          offset = corrected
          env = await fetchPage(offset)
          if (Array.isArray(env)) return
        }
      }
      const older = normalizeMessages(env.items.map(toChatMessage))
      const pageMeta = {
        total: env.total,
        hasMore: Boolean(env.hasMore),
        nextOffset: env.nextOffset ?? null,
        limit: env.limit || meta.limit,
      }
      if (!older.length || typeof firstIndex !== 'number') {
        historyMeta.value[chatId] = pageMeta
        return
      }
      // The page must continue directly above the loaded window; anything
      // else means the session changed underneath us and the next full
      // refresh will resync — don't splice misaligned rows in.
      if (older[0].i === firstIndex - older.length) {
        historyMeta.value[chatId] = pageMeta
        messages.value[chatId] = [...older, ...local]
        persistMessages()
        return
      }
      // Still misaligned after the retry: take the fresher total, but keep the
      // previous `hasMore`/`nextOffset` so scrolling can try again. Adopting a
      // rejected page's `hasMore: false` is what made the remaining history
      // unreachable for the rest of the session.
      historyMeta.value[chatId] = { ...meta, total: env.total }
    } catch {
      // Transient failure: leave state so the user can retry by scrolling.
    } finally {
      delete loadingOlder.value[chatId]
    }
  }

  /** Fetch the full content of one pruned row by absolute index and splice
   * it back into the timeline. Concurrent expands of the same row dedup. */
  function expandMessagePart(chatId: string, index: number): Promise<void> {
    const key = `${chatId}:${index}`
    const existing = partRequests.get(key)
    if (existing) return existing
    const request = (async () => {
      try {
        const row = await api.get<{ content: string }>(`/api/chats/${chatId}/messages/part?i=${index}`)
        const list = messages.value[chatId] || []
        const pos = list.findIndex(m => m.i === index)
        if (pos >= 0) {
          const next = list.slice()
          next[pos] = { ...next[pos], content: row.content, lazy: false }
          messages.value[chatId] = next
          persistMessages()
        }
      } catch {
        // Leave the row collapsed; the marker stays so the user can retry.
      } finally {
        partRequests.delete(key)
      }
    })()
    partRequests.set(key, request)
    return request
  }

  // Post-result reconciliation: the SDK session file is sometimes a beat
  // behind the result event (buffered writes, WS reconnect races). Retry
  // loadMessages until the server's history ends with a final assistant
  // reply, so the bubble lands without needing a manual close/reopen.
  // Background: this fires on every turn while the chat is already open and
  // rendered, so it must not flash the "Updating conversation…" indicator on
  // each of its up-to-6 retries.
  async function reconcileAfterResult(chatId: string) {
    const delays = [0, 300, 700, 1500, 3000, 5000]
    for (const delay of delays) {
      if (delay) await new Promise(r => setTimeout(r, delay))
      await loadMessages(chatId, { background: true })
      const msgs = messages.value[chatId] || []
      const last = msgs[msgs.length - 1]
      // Stop once the turn is capped by a non-error assistant reply or an
      // explicit error/system note — anything that isn't a trailing user msg
      // or tool-activity entry means the final state is rendered.
      if (!last) {
        // A turn can legitimately end with nothing on the transcript (the
        // image-capability pre-flight aborts before dispatch, so /messages
        // stays empty). Retrying cannot change that: clear the stale spinner
        // instead of running out the retry budget with "Thinking…" on screen.
        if (!projectStreaming.value[chatId]) {
          clearStreamingState(chatId)
          void loadSubagents(chatId)
          return
        }
        continue
      }
      if (last.role === 'assistant' && !last.is_error) {
        clearStreamingState(chatId)
        void loadSubagents(chatId)
        return
      }
      if (last.role === 'system' && last.tool_name !== '_activity') {
        clearStreamingState(chatId)
        void loadSubagents(chatId)
        return
      }
    }
    void loadSubagents(chatId)
  }

  // ── Subagents ───────────────────────────────────────────────────────

  async function loadSubagents(chatId: string): Promise<void> {
    try {
      const r = await api.get<SubagentTranscript[]>(`/api/chats/${chatId}/subagents`)
      subagents.value[chatId] = Array.isArray(r) ? r : []
    } catch {
      // No session locally / SDK error — leave any prior data in place.
    }
  }

  // ── Chat switching ──────────────────────────────────────────────────

  function chatExistsInList(chatId: string, list: ChatInfo[] = chats.value): boolean {
    return list.some(ch => ch.chat_id === chatId && !ch.archived)
  }

  async function ensureWorkspaceForChat(chatId: string) {
    const project = projectFor(chatId)
    if (!project || project.workspace === activeWorkspace.value) return
    if (activeChatId.value) disconnectWs(activeChatId.value)
    activeWorkspace.value = project.workspace
    persistState()
  }

  /** Deep-link / tray / notification navigation into a specific chat. */
  async function openChatFromDeepLink(chatId: string) {
    if (!chatExistsInList(chatId)) return
    await ensureWorkspaceForChat(chatId)
    if (activeChatId.value === chatId) {
      // switchChat's "already active" fast path only marks read — fine for
      // e.g. re-clicking the same sidebar entry, but arriving via a
      // notification is exactly the signal that local state may be stale
      // (the socket can go half-open while backgrounded, leaving `streaming`
      // stuck true and the finished reply never pulled in). Force the same
      // reconcile the liveness watchdog and resume-from-background path use.
      const { router } = await import('../router')
      if (router.currentRoute.value.params.chatId !== chatId) {
        router.push(`/chat/${chatId}`)
      }
      void markRead(chatId)
      await reloadAndReconnectChat(chatId)
      return
    }
    await switchChat(chatId)
  }

  /**
   * `skipHistory` is for a chat this client just created: its history is
   * provably empty, so the /messages round-trip can only return nothing while
   * the "Loading conversation" skeleton sits on screen for its duration. Every
   * other entry point still fetches.
   */
  async function switchChat(chatId: string, opts?: { skipHistory?: boolean }) {
    await ensureWorkspaceForChat(chatId)
    // Always sync URL, even if activeChatId already matches (we may have
    // landed here from /settings or /schedules where the chat route isn't
    // currently active).
    const { router } = await import('../router')
    const currentRouteChatId = router.currentRoute.value.params.chatId
    if (currentRouteChatId !== chatId) {
      router.push(`/chat/${chatId}`)
    }
    if (activeChatId.value === chatId) {
      void markRead(chatId)
      return
    }
    // Disconnect old
    if (activeChatId.value) disconnectWs(activeChatId.value)
    activeChatId.value = chatId
    persistState()
    // Fire-and-forget: clears overlay + SW cache + hits /read for cross-device sync.
    void markRead(chatId)
    if (!opts?.skipHistory) await loadMessages(chatId, { waitForSettledReply: true })
    void loadSubagents(chatId)
    connectWs(chatId)
    requestReentrySummaryIfUseful(chatId)
  }

  async function switchWorkspace(ws: WorkspaceName, options?: { transition?: boolean }) {
    const transition = options?.transition !== false
    if (activeWorkspace.value === ws) return
    if (activeChatId.value) disconnectWs(activeChatId.value)
    activeWorkspace.value = ws
    if (transition) {
      // A workspace pill expresses scope, not a request to open an arbitrary
      // conversation. Selecting the first chat here also made the switch (and
      // cross-workspace "new chat") wait for that chat's complete transcript
      // before the home screen could render.
      activeChatId.value = null
      persistState()
      const { router } = await import('../router')
      await router.push('/')
    } else {
      selectFirstChat()
      persistState()
    }
  }

  // ── WebSocket ───────────────────────────────────────────────────────

  function _currentFocused(chatId: string): boolean {
    if (typeof document === 'undefined') return true
    return activeChatId.value === chatId
      && document.visibilityState === 'visible'
      && document.hasFocus()
  }

  function sendFocus(chatId: string) {
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'focus', focused: _currentFocused(chatId) }))
  }

  function connectWs(chatId: string) {
    if (sockets.value[chatId]) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    // chatId comes from server state; pinning it to one encoded path segment
    // keeps a crafted id from rewriting the rest of the WebSocket URL.
    const ws = new WebSocket(`${proto}//${location.host}/ws/chat/${encodeURIComponent(chatId)}`)
    sockets.value[chatId] = ws
    lastChatFrameAt[chatId] = nowMs()
    let opened = false

    ws.onopen = () => {
      if (toRaw(sockets.value[chatId]) !== ws) return
      opened = true
      lastChatFrameAt[chatId] = nowMs()
      sendFocus(chatId)
    }

    ws.onmessage = (ev) => {
      if (toRaw(sockets.value[chatId]) !== ws) return
      // Any frame (including the server keepalive) proves the socket is live.
      lastChatFrameAt[chatId] = nowMs()
      // A working socket clears the reconnect backoff so a later drop starts
      // from a fast first retry again.
      chatReconnectAttempts[chatId] = 0
      const event: WsEvent = JSON.parse(ev.data)
      if (event.type === 'keepalive') {
        hostConnectionUnavailable.value = false
        return
      }
      if (
        event.type !== 'host_unreachable'
        && !(event.type === 'error' && isHostConnectionUnavailableMessage(event.message))
      ) {
        hostConnectionUnavailable.value = false
      }
      // First real frame after a drop/half-open recovery: drop the frozen
      // ephemeral timeline so broker replay rebuilds without duplicating it.
      if (pendingStreamResync.delete(chatId)) {
        streamingText.value[chatId] = ''
        streamingThinking.value[chatId] = ''
        streamingTimeline.value[chatId] = []
        delete streamingTextPhase.value[chatId]
      }
      handleEvent(chatId, event)
    }

    ws.onclose = () => {
      const isCurrent = toRaw(sockets.value[chatId]) === ws
      if (isCurrent) {
        delete sockets.value[chatId]
        delete lastChatFrameAt[chatId]
      }

      const wasIntentional = intentionalCloses.delete(ws)
      if (wasIntentional) return
      if (!isCurrent) return

      // Auto-reconnect the chat the user is actually viewing when the socket
      // drops unexpectedly (server per-turn churn, transient network blip),
      // so live deltas and the final result resume within ~50ms instead of
      // waiting for the 15s poll or a manual reload. Intentional closes
      // (disconnectWs, e.g. switching chats) are skipped.
      if (typeof window === 'undefined' || typeof WebSocket === 'undefined') return
      if (activeChatId.value !== chatId) return

      // Handshake never completed (auth 403 / origin reject). Do not spin
      // reloadAndReconnectChat — that also hammers /messages and fills logs.
      if (!opened) {
        const attempt = (chatReconnectAttempts[chatId] = (chatReconnectAttempts[chatId] || 0) + 1)
        if (attempt >= 5) {
          void api.get('/api/projects').catch(() => {})
          return
        }
        const delay = Math.min(2000 * 2 ** Math.min(attempt, 5), 64000)
        if (chatReconnectTimers[chatId]) window.clearTimeout(chatReconnectTimers[chatId])
        chatReconnectTimers[chatId] = window.setTimeout(() => {
          delete chatReconnectTimers[chatId]
          if (activeChatId.value === chatId && !sockets.value[chatId]) {
            connectWs(chatId)
          }
        }, delay)
        return
      }

      // Keep the live Activity/timeline frozen across the gap. Clearing it
      // here made mid-turn drops look like a hard disconnect even though the
      // server broker was still running.
      pendingStreamResync.add(chatId)
      const attempt = (chatReconnectAttempts[chatId] = (chatReconnectAttempts[chatId] || 0) + 1)
      const delay = chatWsReconnectDelayMs(attempt)
      if (chatReconnectTimers[chatId]) window.clearTimeout(chatReconnectTimers[chatId])
      chatReconnectTimers[chatId] = window.setTimeout(() => {
        delete chatReconnectTimers[chatId]
        // Only if still the viewed chat and not reconnected in the meantime.
        if (activeChatId.value === chatId && !sockets.value[chatId]) {
          void reloadAndReconnectChat(chatId)
        }
      }, delay)
    }
  }

  function checkPendingTarget() {
    if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return
    const ctrl = navigator.serviceWorker.controller
    if (!ctrl) return
    try {
      ctrl.postMessage({ type: 'get-pending-target' })
    } catch { /* ignore */ }
  }

  // Re-pull authoritative history then reconnect the per-chat WS. Used on
  // resume-from-background and by the liveness watchdog: a bare reconnect can
  // miss events the broker already flushed, so loadMessages first, then let
  // connectWs replay whatever the broker still buffers on top.
  async function reloadAndReconnectChat(chatId: string) {
    pendingStreamResync.add(chatId)
    disconnectWs(chatId)
    // Re-attach immediately so an in-flight broker stream can replay while
    // /messages catches up in parallel. Waiting on history first left the UI
    // frozen for the full round-trip on every blip.
    connectWs(chatId)
    void markRead(chatId)
    try {
      await loadMessages(chatId, { background: true })
      if (
        streaming.value[chatId]
        && !projectStreaming.value[chatId]
        && !queuedMessages.value[chatId]?.length
        && hasSettledHistory(chatId)
      ) {
        clearStreamingState(chatId)
        pendingStreamResync.delete(chatId)
      }
    } finally {
      void loadSubagents(chatId)
    }
    // History is authoritative now: a send the server never received has no
    // row here. Give the just-reconnected socket a moment to replay any
    // buffered user_echo, then recover the provably-lost send.
    reconcileUnackedSend(chatId)
    scheduleUnackedSendRecovery(chatId)
  }

  // Detect and recover half-open sockets (readyState OPEN, no keepalive for
  // WS_STALE_MS). Closing the events socket triggers its onclose→reconnect,
  // whose `snapshot` reconciles any missed turn/subagent state; the per-chat
  // socket is reloaded+reconnected so a result delivered during the dead
  // window shows up without the user having to send a message.
  function checkWsLiveness() {
    if (typeof WebSocket === 'undefined') return
    const now = nowMs()
    const ews = eventsSocket.value
    if (ews && ews.readyState === WebSocket.OPEN && lastEventsFrameAt && now - lastEventsFrameAt > WS_STALE_MS) {
      lastEventsFrameAt = now // don't re-fire before the reconnect lands
      try { ews.close() } catch { /* ignore */ }
    }
    const chatId = activeChatId.value
    if (chatId) {
      const cws = sockets.value[chatId]
      const seen = lastChatFrameAt[chatId]
      if (cws && cws.readyState === WebSocket.OPEN && seen && now - seen > WS_STALE_MS) {
        lastChatFrameAt[chatId] = now
        void reloadAndReconnectChat(chatId)
      }
    }
  }

  if (typeof document !== 'undefined') {
    // On resume from background we must both reconnect sockets AND re-pull
    // the persisted history: if the assistant reply landed while the PWA
    // was suspended (e.g. user tapped a "chat ready" notification), the
    // broker may have already flushed its replay buffer, so a bare WS
    // reconnect brings no events and the UI stays on the stale state.
    // Mirrors what switchChat does, so users don't have to re-tap the
    // chat in the sidebar.
    // A const arrow rather than a function declaration: no-inner-declarations
    // objects to hoisting a declaration out of this block, and both callers
    // below run from listeners, well after this line.
    const resumeActiveChat = async () => {
      const chatId = activeChatId.value
      if (chatId) {
        await reloadAndReconnectChat(chatId)
      }
      const ews = eventsSocket.value
      if (ews && ews.readyState === WebSocket.OPEN) {
        try { ews.close() } catch { /* ignore */ }
      } else {
        // Covers both null and a leftover CONNECTING/CLOSING/CLOSED socket. A
        // long sleep/background period can close the socket without ever
        // invoking onclose (JS execution was frozen), leaving eventsSocket
        // pointing at a dead object that this check used to ignore — no
        // snapshot ever arrived to correct a chat left showing "in progress"
        // after its turn actually finished. connectEventsWs() itself no-ops
        // if a live CONNECTING/OPEN socket already exists, so this is safe
        // to call unconditionally.
        connectEventsWs()
      }
    }

    // Liveness watchdog: cheap timer, only acts on genuinely stale sockets.
    window.setInterval(checkWsLiveness, WS_LIVENESS_CHECK_MS)

    // WKWebView can keep the document visible while the native window loses
    // key focus (another app, hide, or minimize). Report those standard
    // browser focus transitions so the engine does not suppress a native
    // notification for a chat the user cannot currently see.
    window.addEventListener('focus', () => {
      if (activeChatId.value) sendFocus(activeChatId.value)
    })
    window.addEventListener('blur', () => {
      if (activeChatId.value) sendFocus(activeChatId.value)
    })

    document.addEventListener('visibilitychange', () => {
      documentVisible.value = document.visibilityState === 'visible'
      if (document.visibilityState === 'visible') {
        // iOS Safari / WKWebView suspends JS and sockets when the PWA
        // is backgrounded (screen lock, home button). On resume the
        // WebSockets can be silently dead — `readyState` may still
        // report OPEN, but no messages flow.
        void resumeActiveChat()
        void syncLatest()
        // A claim expires server-side, and a window left open on one chat never
        // changes its own boolean, so nothing else would renew it. This handler
        // and the pageshow one below are the app's existing wake points; adding
        // a third listener elsewhere would just split wake handling further.
        const chatId = (() => {
          if (typeof window === 'undefined') return undefined
          return window.location.pathname.match(/^\/chat\/([^/]+)/)?.[1]
        })()
        if (chatId) void openChatFromDeepLink(chatId)
      } else if (activeChatId.value) {
        // Visibility → hidden: just notify the server of focus state.
        sendFocus(activeChatId.value)
      }
    })

    // pageshow fires when the PWA is restored from the bfcache (iOS
    // home→back pattern). visibilitychange often doesn't fire in that
    // case, so force-refresh here too.
    window.addEventListener('pageshow', (ev) => {
      if ((ev as PageTransitionEvent).persisted || document.visibilityState === 'visible') {
        void resumeActiveChat()
        checkPendingTarget()
      }
    })

    if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
      navigator.serviceWorker.addEventListener('message', (ev) => {
        const data = ev.data
        if (data && data.type === 'open-chat' && data.chat_id) {
          void openChatFromDeepLink(data.chat_id)
        } else if (data && data.type === 'pending-target' && data.chat_id) {
          void openChatFromDeepLink(data.chat_id)
        }
      })
    }
  }

  function disconnectWs(chatId: string) {
    // Cancel any pending auto-reconnect and mark this as an intentional close
    // so onclose does not schedule a new one.
    if (chatReconnectTimers[chatId]) {
      window.clearTimeout(chatReconnectTimers[chatId])
      delete chatReconnectTimers[chatId]
    }
    const ws = toRaw(sockets.value[chatId])
    if (ws) {
      intentionalCloses.add(ws)
      ws.close()
      delete sockets.value[chatId]
    }
  }

  // ── Global events WS (cross-chat awareness) ─────────────────────────

  function beginServerRestart(message?: string) {
    if (serverRestarting.value) return
    serverRestarting.value = true
    serverRestartMessage.value = restartMessageForDisplay(message)
    void reloadWhenServerReady()
  }

  function undoOptimisticSend(chatId: string) {
    // A send that was rejected for restart drain already pushed a local user
    // bubble and flipped streaming on. Roll that back so the chat doesn't
    // keep a phantom turn / "Fix this error" affordance.
    const msgs = messages.value[chatId]
    if (msgs && msgs.length > 0) {
      const last = msgs[msgs.length - 1]
      if (last.role === 'user') {
        messages.value[chatId] = msgs.slice(0, -1)
        persistMessages()
      }
    }
    streaming.value[chatId] = false
    streamingText.value[chatId] = ''
    streamingThinking.value[chatId] = ''
    delete streamingTextPhase.value[chatId]
    delete liveUsage.value[chatId]
    delete streamStartedAt.value[chatId]
    persistStreamStartedAt()
  }

  // Consecutive handshakes that closed without ever opening. A server that
  // rejects the upgrade (403 after a token rotation or restart) fails
  // identically on every attempt, so a fixed 2s retry becomes a request
  // storm that fills the server log.
  let eventsWsFailureStreak = 0

  function connectEventsWs() {
    if (eventsSocket.value && eventsSocket.value.readyState <= WebSocket.OPEN) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/events`)
    eventsSocket.value = ws
    lastEventsFrameAt = nowMs()
    let opened = false

    ws.onopen = () => {
      if (toRaw(eventsSocket.value) !== ws) return
      opened = true
      eventsWsFailureStreak = 0
      lastEventsFrameAt = nowMs()
    }

    ws.onmessage = (ev) => {
      if (toRaw(eventsSocket.value) !== ws) return
      // Any frame (including the server keepalive) proves the socket is live.
      lastEventsFrameAt = nowMs()
      let msg: EventsWsMessage
      try { msg = JSON.parse(ev.data) } catch { return }
      if (msg.type === 'keepalive') return
      handleEventsMessage(msg)
    }

    ws.onclose = () => {
      const isCurrent = toRaw(eventsSocket.value) === ws
      if (isCurrent) {
        eventsSocket.value = null
      }
      if (!isCurrent) return

      if (opened) {
        eventsWsFailureStreak = 0
        // A previously-live awareness socket should come back immediately so
        // chat_streaming_done / result_ready are not delayed after a blip.
        setTimeout(() => {
          if (!eventsSocket.value) connectEventsWs()
        }, 50)
        return
      }
      eventsWsFailureStreak += 1
      if (eventsWsFailureStreak >= 5) {
        // Likely an auth rejection: probe the HTTP API so its 401
        // handling can redirect this stale tab to /login. Re-probe on every
        // failure past the threshold (not just the 5th) so a tab that keeps
        // flapping still gets redirected.
        void api.get('/api/projects').catch(() => {})
      }
      // Reconnect with exponential backoff on repeated handshake failures
      // (2s → 64s cap); cross-chat awareness is best-effort.
      const delay = Math.min(2000 * 2 ** Math.min(eventsWsFailureStreak, 5), 64000)
      setTimeout(() => {
        if (!eventsSocket.value) connectEventsWs()
      }, delay)
    }

    ws.onerror = () => {
      try { ws.close() } catch { /* ignore */ }
    }
  }

  // A single loop lifecycle call can fan out several `loops_changed` frames
  // (create-then-start), and each one would otherwise cost a full GET /api/loops
  // per open tab. Coalesce a burst into one refetch. The tasks store is imported
  // lazily to keep it out of this module's import graph.
  let loopsRefetchTimer: ReturnType<typeof setTimeout> | null = null
  function scheduleLoopsRefetch(): void {
    if (loopsRefetchTimer !== null) return
    loopsRefetchTimer = setTimeout(() => {
      loopsRefetchTimer = null
      void import('./tasks')
        .then(({ useTaskStore }) => useTaskStore().fetchLoops())
        .catch(() => {})
    }, 150)
  }

  function handleEventsMessage(msg: EventsWsMessage) {
    switch (msg.type) {
      case 'snapshot': {
        // Reset broker-streaming state to match server truth.
        projectStreaming.value = {}
        for (const entry of msg.active_streams) {
          projectStreaming.value[entry.chat_id] = true
        }
        // Authoritative background-agent counts: replace local state so a
        // count left stale by a missed event (WS gap, server restart) heals
        // on reconnect.
        backgroundAgents.value = { ...(msg.background_agents || {}) }
        // Post-archive pipelines still in flight. Authoritative like the counts
        // above: a chat the server no longer lists as running has settled, so
        // clear a stale 'running' rather than leaving it pulsing forever.
        applyPostprocessingSnapshot(msg.postprocessing || [])
        if (msg.restarting) {
          beginServerRestart()
        }
        // Recovery: if we locally think the active chat is streaming but
        // the snapshot shows no stream is running for it, the turn ended
        // server-side while our events socket was disconnected (and the
        // per-chat WS likely also missed the result). Refetch /messages
        // so the UI doesn't stay stuck on the prior turn / "Working..."
        // until a manual reload.
        const activeForSnap = activeChatId.value
        if (activeForSnap && streaming.value[activeForSnap] && !projectStreaming.value[activeForSnap]) {
          void reconcileAfterResult(activeForSnap)
        }
        break
      }
      case 'server_restarting':
        beginServerRestart(msg.message)
        break
      case 'chat_streaming_started':
        projectStreaming.value[msg.chat_id] = true
        // Note: backgroundAgents is NOT cleared here — agents from a prior
        // turn keep running across new turns. The server's JSONL watcher
        // re-announces the count at every turn end (including 0), and the
        // events snapshot heals stale counts on reconnect.
        if (
          msg.chat_id === activeChatId.value &&
          shouldReconnectActiveChatOnStreamingStarted(sockets.value[msg.chat_id])
        ) {
          disconnectWs(msg.chat_id)
          connectWs(msg.chat_id)
        }
        break
      case 'chat_streaming_done': {
        delete projectStreaming.value[msg.chat_id]
        // The per-chat WS may have missed the `result` event for this
        // turn (WS flap mid-stream, or the broker stream finished and
        // was cleared between the disconnect and reconnect, leaving no
        // events to replay). When that happens the local UI is stuck
        // showing the prior turn with the streaming spinner on. Reconcile
        // against /messages so the new assistant bubble shows up without
        // requiring a manual refresh. Limit to the active chat — inactive
        // chats refetch on their next open via switchChat → loadMessages.
        if (msg.chat_id === activeChatId.value) {
          const localMsgs = messages.value[msg.chat_id]
          const last = localMsgs && localMsgs.length > 0 ? localMsgs[localMsgs.length - 1] : null
          const turnSettled = last !== null && last.role === 'assistant' && !last.is_error
          if (!turnSettled || streaming.value[msg.chat_id]) {
            void reconcileAfterResult(msg.chat_id)
          }
        } else if (streaming.value[msg.chat_id]) {
          // Inactive chat finished. Its per-chat WS was detached when the user
          // switched away, so the `result` event that normally clears the local
          // optimistic `streaming` flag will never arrive — leaving
          // isChatStreaming() (projectStreaming || streaming) true and the
          // sidebar dot stuck "working" until a full reload. The server has
          // declared the turn done, so clear the local streaming state now.
          // The chat's final history is refetched on its next open.
          clearStreamingState(msg.chat_id)
        }
        break
      }
      case 'chat_result_ready': {
        const resultChat = chats.value.find(c => c.chat_id === msg.chat_id)
        // The server suppresses delegate result events, but keep this guard
        // for older servers and replayed events so internal child work cannot
        // leak into the notification tray.
        if (resultChat && isNestedDelegate(resultChat)) break
        const isFocused = activeChatId.value === msg.chat_id &&
          (typeof document === 'undefined' || document.visibilityState === 'visible')
        if (isFocused) {
          // User is looking at this chat right now. Advance server read state
          // so the delayed-push scheduler skips this chat and our other
          // devices clear their unread automatically.
          void markRead(msg.chat_id)
        } else {
          // Optimistic local flag: binary, hydrated by the server fetch below.
          unread.value[msg.chat_id] = 1
          persistUnread()
          // In-app toast for the document-visible-but-different-chat case.
          if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
            pushToast({
              chat_id: msg.chat_id,
              title: msg.title || 'ciaobot',
              body: msg.snippet || 'New message',
            })
          }
        }
        // Refresh the chats list so last_activity_at + recent ordering update.
        api.get<ChatInfo[]>('/api/chats?active_only=1')
          .then(c => reconcileActiveChats(c))
          .catch(() => { /* ignore */ })
        break
      }
      case 'chat_subagents_ready': {
        const prevAgents = backgroundAgents.value[msg.chat_id] || 0
        if (msg.remaining > 0) {
          backgroundAgents.value[msg.chat_id] = msg.remaining
        } else {
          delete backgroundAgents.value[msg.chat_id]
        }
        // A non-decreasing positive count is the initial "N running"
        // announcement (or a subagent spawning children). It does not warrant
        // a full history reconcile (no new agent output yet), but we do want
        // the transcript panel to populate promptly so a freshly dispatched
        // agent is visible without waiting up to 4s for the poll watcher's
        // first tick. Pull subagents once (focused only) then return.
        if (msg.remaining >= prevAgents && msg.remaining > 0) {
          const focusedNow = activeChatId.value === msg.chat_id &&
            (typeof document === 'undefined' || document.visibilityState === 'visible')
          if (focusedNow) void loadSubagents(msg.chat_id)
          break
        }
        const isFocused = activeChatId.value === msg.chat_id &&
          (typeof document === 'undefined' || document.visibilityState === 'visible')
        if (isFocused) {
          // Subagent transcripts land after the parent turn's result. Refresh
          // history and the subagent panel so the user sees the update without
          // having to switch chats or wait for the next sync interval.
          void reconcileAfterResult(msg.chat_id)
          void loadSubagents(msg.chat_id)
        }
        // Keep sidebar ordering and last-activity timestamps in sync.
        api.get<ChatInfo[]>('/api/chats?active_only=1')
          .then(c => reconcileActiveChats(c))
          .catch(() => { /* ignore */ })
        break
      }
      case 'chat_read': {
        // Another tab/device marked this chat read: sync our state and
        // clear the SW cache entry so the native badge stays accurate.
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) chat.last_read_at = msg.last_read_at
        if (unread.value[msg.chat_id]) {
          delete unread.value[msg.chat_id]
          persistUnread()
        }
        postServiceWorkerMessage({ type: 'chat-focused', chat_id: msg.chat_id })
        break
      }
      case 'chat_created': {
        // A new chat (fresh or fork) was created on this instance. Other
        // tabs/devices have no other real-time signal for this: create/fork
        // emit no streaming event, so without this handler the sidebar only
        // learns about the chat via the 15s syncLatest poll or a manual
        // refresh. replaceChat is idempotent (update-in-place if we already
        // pushed optimistically, push otherwise).
        replaceChat(msg.chat)
        break
      }
      case 'chat_title': {
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) {
          chat.title = msg.title
          // Server emits status='pending' when a title generation is
          // in flight (shows shimmer placeholder), status='ready' (or
          // omitted, for back-compat) once the final title arrives.
          chat.title_status = msg.status ?? 'ready'
        }
        break
      }
      case 'chat_moved': {
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) chat.project_id = msg.project_id
        break
      }
      case 'chat_retry': {
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) {
          chat.retry = msg.status ? {
            status: msg.status,
            next_at: msg.next_at || '',
            last_error: msg.last_error || '',
            attempts: msg.attempts || 0,
            interval_seconds: msg.interval_seconds || 3600,
          } : null
        }
        break
      }
      case 'chat_archived': {
        disconnectWs(msg.chat_id)
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) {
          chat.archived = true
          if (msg.archive_path) chat.archive_path = msg.archive_path
        }
        if (archivingChats.value[msg.chat_id]) delete archivingChats.value[msg.chat_id]
        // Hold the local flag until a chat-list payload agrees: a GET that left
        // before the archive committed can still resolve after this event.
        pendingArchived.value.add(msg.chat_id)
        if (activeChatId.value === msg.chat_id) {
          activeChatId.value = null
          persistState()
        }
        break
      }
      case 'chat_postprocess': {
        // The post-archive pipeline reporting itself: which step is running, and
        // once it settles, what it produced. Written onto the chat so the
        // archived transcript keeps the record after the events stop.
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) chat.postprocess = msg.postprocess || null
        break
      }
      case 'chat_deleted': {
        // Fires when the server prunes an empty chat (user created a "New
        // Chat" and never sent a message, then moved on) or when another
        // tab issued an explicit DELETE. Drop the row and detach the active
        // selection if it was the one removed.
        chats.value = chats.value.filter(c => c.chat_id !== msg.chat_id)
        if (activeChatId.value === msg.chat_id) {
          activeChatId.value = null
        }
        if (messages.value[msg.chat_id]) delete messages.value[msg.chat_id]
        if (subagents.value[msg.chat_id]) delete subagents.value[msg.chat_id]
        if (streaming.value[msg.chat_id]) delete streaming.value[msg.chat_id]
        if (streamingText.value[msg.chat_id]) delete streamingText.value[msg.chat_id]
        delete streamingTextPhase.value[msg.chat_id]
        if (queuedMessages.value[msg.chat_id]) delete queuedMessages.value[msg.chat_id]
        if (unread.value[msg.chat_id]) {
          delete unread.value[msg.chat_id]
          persistUnread()
        }
        break
      }
      case 'open_chat':
        void openChatFromDeepLink(msg.chat_id)
        break
      case 'project_created': {
        const exists = projects.value.some(p => p.project_id === msg.project.project_id)
        if (!exists) projects.value.push(msg.project)
        break
      }
      case 'project_updated': {
        const idx = projects.value.findIndex(p => p.project_id === msg.project.project_id)
        if (idx >= 0) projects.value[idx] = msg.project
        else projects.value.push(msg.project)
        break
      }
      case 'project_deleted': {
        projects.value = projects.value.filter(p => p.project_id !== msg.project_id)
        chats.value = chats.value.filter(c => c.project_id !== msg.project_id)
        if (activeChat.value && activeChat.value.project_id === msg.project_id) {
          activeChatId.value = null
        }
        break
      }
      case 'projects_reordered': {
        // Server-authoritative order after a drag-reorder (this or another
        // device). Rewrite local order so workspaceProjects re-sorts.
        const orderMap = new Map<string, number>(
          (msg.order as string[]).map((pid, i) => [pid, i]),
        )
        projects.value.forEach(p => {
          const next = orderMap.get(p.project_id)
          if (next !== undefined) p.order = next
        })
        break
      }
      case 'loops_changed': {
        // A loop was created/edited/started/stopped/deleted elsewhere (the
        // model mid-turn, the Schedules page, another tab). Refetch so the
        // chat's loop banner and the sidebar/home loop markers appear without
        // a manual reload.
        scheduleLoopsRefetch()
        break
      }
      case 'gws_health': {
        // A Google Workspace login went dead (revoked/expired token). The
        // server debounces to one event per breakage; surface it as a
        // persistent error toast. The fix is re-authentication in
        // Settings → Workspaces, so the Fix action navigates there rather
        // than seeding a chat. The PWA push/menu-bar banner is the other
        // channel (see push.py); this is the live in-app signal.
        pushErrorToast(msg.title || 'Google Workspace login needs attention', msg.body || '', {
          fixRoute: '/settings/workspaces',
          fixLabel: 'Fix in Settings',
        })
        break
      }
    }
  }

  // ── Send messages ───────────────────────────────────────────────────

  // Render pending comments as XML-tagged reference blocks (see
  // lib/commentContext.ts for the format and rationale). The model gets an
  // unambiguous boundary around the file/line anchor, the verbatim selection,
  // and the user's note; the same tags are whitelisted in the renderer and
  // styled as quote cards so they read cleanly in the chat bubble too.
  function formatPendingComments(comments = pendingComments.value): string {
    return formatFileComments(comments)
  }

  function formatPendingChatComments(comments = pendingChatComments.value): string {
    return formatChatComments(comments)
  }

  type PreparedMessage = {
    composed: string
    imageRefs?: string[]
    fileComments: PendingComment[]
    chatComments: PendingChatComment[]
  }

  function prepareMessage(chatId: string, text: string): PreparedMessage {
    const chatImages = getPendingBucket(pendingImagesByChat.value, chatId)
    const fileComments = getPendingBucket(pendingCommentsByChat.value, chatId)
    const chatComments = getPendingBucket(pendingChatCommentsByChat.value, chatId)
    // Collect images from pendingImages plus any images attached to comments.
    const allImages = new Set<string>(chatImages)
    for (const c of fileComments) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    for (const c of chatComments) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    const imageRefs = allImages.size > 0 ? Array.from(allImages) : undefined
    const fileBlock = formatPendingComments(fileComments)
    const chatBlock = formatPendingChatComments(chatComments)
    const hasTyped = text.trim().length > 0
    // Reference blocks (quoted text + note) go FIRST, then the typed prompt,
    // so the model reads the material being discussed before the instruction
    // (Anthropic: placing the query at the end of the input improves quality).
    let composed = ''
    if (fileBlock) composed += fileBlock
    if (chatBlock) composed += (composed ? '\n' : '') + chatBlock
    if (hasTyped) composed += (composed ? '\n\n' : '') + text.trim()
    return { composed, imageRefs, fileComments, chatComments }
  }

  function consumePreparedAttachments(chatId: string, message: PreparedMessage) {
    setPendingBucket<string>(pendingImagesByChat.value, chatId, [])
    persistPendingImages()
    // Remove sent file comments from the durable store so they don't linger
    // in the viewer after the message has been dispatched.
    for (const c of message.fileComments) {
      const list = fileComments.value[c.path]
      if (list) {
        const next = list.filter(x => x.id !== c.id)
        if (next.length) fileComments.value[c.path] = next
        else delete fileComments.value[c.path]
      }
    }
    persistFileComments()
    setPendingBucket<PendingComment>(pendingCommentsByChat.value, chatId, [])
    setPendingBucket<PendingChatComment>(pendingChatCommentsByChat.value, chatId, [])
    persistPendingComments()
    persistPendingChatComments()
  }

  // Deferred-send retry limit. When the chat WS is down, sendMessage defers
  // the actual WS send by 500ms and retries. Without a cap this loops forever
  // when the server is unreachable, keeping the composer frozen and never
  // surfacing an error. After this many attempts the deferred send is
  // abandoned and a system error bubble tells the user the message didn't go.
  const DEFERRED_SEND_MAX_RETRIES = 20

  // ── Deferred sends ──────────────────────────────────────────────────
  // A send made while the chat socket is down waits on that 500ms retry
  // chain. It used to render *nothing at all* in the meantime: no bubble, no
  // pending marker, and the composer kept the text because sendMessage
  // returned false. For up to 10s the user had no evidence their message was
  // accepted, so they pressed send again — and each press started its own
  // independent chain with no de-duplication. When the socket finally opened
  // every chain fired; a turn was running by then, so they all took the
  // `alreadyStreaming` queue branch and N identical entries appeared in the
  // queue at once ("many times the same message queued").
  //
  // Two things fix that: render the message immediately as an optimistic
  // bubble, and keep a per-chat registry of the sends still waiting so a
  // re-send can collapse into the one already in flight.
  //
  // `pendingSend` marks a bubble that is rendered but not yet on the wire, so
  // the send-out path can promote that exact bubble in place instead of
  // pushing a second copy. It deliberately carries no `turn_index`, which is
  // the invariant the `user_echo` handler relies on to reconcile an
  // optimistic bubble rather than duplicate the turn.
  type PendingUserMessage = ChatMessage & { pendingSend?: true }
  interface DeferredSend { composed: string; images?: string[] }
  const deferredSends: Record<string, DeferredSend[]> = {}

  function sameDeferredPayload(a: DeferredSend, b: DeferredSend): boolean {
    if (a.composed.trim() !== b.composed.trim()) return false
    const ai = a.images || []
    const bi = b.images || []
    return ai.length === bi.length && ai.every((v, i) => v === bi[i])
  }

  function findDeferredSend(chatId: string, entry: DeferredSend): DeferredSend | undefined {
    return deferredSends[chatId]?.find(d => sameDeferredPayload(d, entry))
  }

  function registerDeferredSend(chatId: string, entry: DeferredSend) {
    if (!deferredSends[chatId]) deferredSends[chatId] = []
    deferredSends[chatId].push(entry)
  }

  function dropDeferredSend(chatId: string, entry: DeferredSend) {
    const list = deferredSends[chatId]
    if (!list) return
    const idx = list.findIndex(d => sameDeferredPayload(d, entry))
    if (idx === -1) return
    list.splice(idx, 1)
    if (!list.length) delete deferredSends[chatId]
  }

  function pushPendingSendBubble(chatId: string, entry: DeferredSend) {
    const msgs = messages.value[chatId] || []
    const bubble: PendingUserMessage = {
      role: 'user',
      content: entry.composed,
      timestamp: new Date().toISOString(),
      images: entry.images,
      pendingSend: true,
    }
    msgs.push(bubble)
    messages.value[chatId] = msgs
    persistMessages()
  }

  // A history reload (mergeMetadata keeps only turns the server knows about)
  // can wipe the optimistic bubble while the retry chain is still waiting, so
  // every caller must tolerate "not found". Matching on the whole payload, not
  // just the text, keeps two same-text sends carrying different attachments
  // from promoting or deleting each other's bubble.
  function findPendingSendBubble(chatId: string, entry: DeferredSend): number {
    const msgs = messages.value[chatId] || []
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i] as PendingUserMessage
      if (m.role !== 'user' || !m.pendingSend) continue
      if (sameDeferredPayload({ composed: m.content || '', images: m.images }, entry)) return i
    }
    return -1
  }

  // The frame is on the wire (or the send was abandoned): this is an ordinary
  // optimistic bubble now. Clearing the marker keeps a later deferred send
  // from promoting or deleting a bubble that is not its own, and guarantees
  // no bubble can stay "pending" forever.
  function settlePendingSendBubble(chatId: string, entry: DeferredSend): boolean {
    const idx = findPendingSendBubble(chatId, entry)
    if (idx === -1) return false
    delete (messages.value[chatId][idx] as PendingUserMessage).pendingSend
    return true
  }

  function dropPendingSendBubble(chatId: string, entry: DeferredSend) {
    const idx = findPendingSendBubble(chatId, entry)
    if (idx === -1) return
    messages.value[chatId].splice(idx, 1)
  }

  function sendMessage(
    chatId: string,
    text: string,
    prepared?: PreparedMessage,
    onSent?: () => void,
    _deferredAttempt = 0,
  ): boolean {
    // A re-entry summary is a transient orientation aid, not a new chat
    // message. The first send is the user's signal that it has done its job.
    clearReentrySummary(chatId)
    // Any send implicitly answers (or dismisses) a pending AskUserQuestion
    // picker — the model already got an empty tool result and is reading
    // this turn for the actual answer. Clear the local chat's persisted
    // pending_question too, so a loadMessages racing this send (WS reconnect,
    // reconciliation) doesn't rebuild the picker from a now-stale value.
    if (activeQuestions.value[chatId]) {
      markResolvedQuestion(chatId)
      delete activeQuestions.value[chatId]
    }
    // A send also implicitly dismisses any open image-capability question:
    // the user is re-sending through the normal path (e.g. after opening the
    // model picker), so the stale card must not linger.
    if (activeCapabilityQuestions.value[chatId]) {
      delete activeCapabilityQuestions.value[chatId]
    }
    const answeredChat = chats.value.find(c => c.chat_id === chatId)
    if (answeredChat?.pending_question) answeredChat.pending_question = ''
    const message = prepared || prepareMessage(chatId, text)
    // A reconnect can outlive the user's next edit. Freeze the complete
    // attachment bundle now, before a retry callback can read another
    // message's staged attachments from the shared composer bucket.
    if (!prepared) consumePreparedAttachments(chatId, message)
    const { composed, imageRefs } = message
    // Only a retry can own an optimistic pending bubble / registry entry: the
    // first call either sends straight away or creates them below.
    const wasDeferred = _deferredAttempt > 0
    const deferredEntry: DeferredSend = { composed, images: imageRefs }
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (!wasDeferred) {
        // Re-send de-duplication. An identical payload already waiting on the
        // socket collapses into that send instead of starting a second retry
        // chain — that stacking is what produced N copies of the same message
        // in the queue. Only an *identical* payload collapses; a genuinely
        // different message still gets its own chain, so nothing is dropped.
        if (findDeferredSend(chatId, deferredEntry)) {
          // The message is already visible as a pending bubble, so treat the
          // press as accepted: clearing the composer stops it from holding
          // text that invites yet another re-send.
          onSent?.()
          return true
        }
        registerDeferredSend(chatId, deferredEntry)
        // Render it now. Nothing appeared here until the socket opened, which
        // is exactly what "I sent the message, nothing happens" meant.
        pushPendingSendBubble(chatId, deferredEntry)
        onSent?.()
      }
      // Retry limit: if the socket stays down, abandon the deferred send and
      // surface a system error bubble so the user knows the message didn't
      // go through.
      if (_deferredAttempt >= DEFERRED_SEND_MAX_RETRIES) {
        dropDeferredSend(chatId, deferredEntry)
        // Keep the user's message in the transcript — the composer was cleared
        // when we accepted it, so this bubble is their only copy, and
        // ChatPanel's error retry resends the user turn above the error — but
        // stop calling it pending now that no further attempt is coming.
        settlePendingSendBubble(chatId, deferredEntry)
        const errorMsgs = messages.value[chatId] || []
        errorMsgs.push({
          role: 'system',
          content: 'Error: Could not send — chat connection is down. Please retry.',
          timestamp: new Date().toISOString(),
        })
        messages.value[chatId] = errorMsgs
        streaming.value[chatId] = false
        delete streamStartedAt.value[chatId]
        persistStreamStartedAt()
        return false
      }
      connectWs(chatId)
      setTimeout(
        () => sendMessage(chatId, text, message, onSent, _deferredAttempt + 1),
        500,
      )
      // Accepted, not sent: the bubble is on screen and the composer is
      // clear. Returning false here left the text in the composer with no
      // bubble anywhere, which read as "nothing happened".
      return true
    }
    const alreadyStreaming = isChatStreaming(chatId)

    if (alreadyStreaming) {
      // Queue: don't push to the main messages list yet. Queued messages live
      // in queuedMessages until the server echoes them.
      if (wasDeferred) {
        // This deferred send is becoming a real queue entry. Drop its
        // optimistic bubble so the message isn't rendered twice (bubble in
        // the transcript *and* a queued chip).
        dropDeferredSend(chatId, deferredEntry)
        dropPendingSendBubble(chatId, deferredEntry)
      }
      if (!queuedMessages.value[chatId]) queuedMessages.value[chatId] = []
      const queueId = makeQueuedId()
      queuedMessages.value[chatId].push({ id: queueId, text: composed, images: imageRefs })
      const payload: Record<string, unknown> = { type: 'message', text: composed, mode: 'queue' }
      if (imageRefs) payload.images = imageRefs
      payload.entry_id = queueId
      ws.send(JSON.stringify(payload))
      onSent?.()
      return true
    }

    // A deferred send already rendered its message; promote that same bubble
    // in place rather than pushing a second copy. `settlePending…` returns
    // false when a history reload wiped it, in which case we push fresh.
    if (wasDeferred) dropDeferredSend(chatId, deferredEntry)
    if (!wasDeferred || !settlePendingSendBubble(chatId, deferredEntry)) {
      const msgs = messages.value[chatId] || []
      msgs.push({
        role: 'user',
        content: composed,
        timestamp: new Date().toISOString(),
        images: imageRefs,
      })
      messages.value[chatId] = msgs
    }
    // Persist immediately so the user's own message survives app close even
    // if the assistant response never arrives (dropped WS, closed window).
    persistMessages()
    streaming.value[chatId] = true
    streamingText.value[chatId] = ''
    streamingThinking.value[chatId] = ''
    delete streamingTextPhase.value[chatId]
    streamStartedAt.value[chatId] = Date.now()
    persistStreamStartedAt()
    delete liveUsage.value[chatId]

    const payload: Record<string, unknown> = { type: 'message', text: composed }
    if (imageRefs) payload.images = imageRefs
    // Track until the server proves receipt. Cleared by the echoed user_echo,
    // by the turn showing up in /messages, or recovered once on reconnect.
    // An identical pending entry (the recovery path resending this very
    // message) keeps its attempt count so the one-retry cap holds.
    const tracked = unackedSends[chatId]
    if (!tracked || tracked.text !== composed) {
      unackedSends[chatId] = { text: composed, images: imageRefs, at: Date.now(), attempts: 0 }
      persistUnackedSends()
    }
    ws.send(JSON.stringify(payload))
    onSent?.()
    return true
  }

  function removeQueued(chatId: string, index: number) {
    const list = queuedMessages.value[chatId]
    if (!list) return
    const entry = list[index]
    if (!entry) return
    list.splice(index, 1)
    if (!list.length) delete queuedMessages.value[chatId]
    const ws = sockets.value[chatId]
    if (ws?.readyState === WebSocket.OPEN && entry?.id) {
      ws.send(JSON.stringify({ type: 'queue_remove', entry_id: entry.id }))
    }
  }

  function removeQueuedById(chatId: string, entryId: string) {
    const list = queuedMessages.value[chatId]
    if (!list) return
    const idx = list.findIndex(q => q.id === entryId)
    if (idx === -1) return
    list.splice(idx, 1)
    if (!list.length) delete queuedMessages.value[chatId]
  }

  function removeEchoedQueued(chatId: string, entryId: string | undefined, text: string) {
    const list = queuedMessages.value[chatId]
    if (!list?.length) return
    // Newer servers identify the exact queue item that started. The text
    // fallback keeps rolling upgrades working, while removing only one match
    // preserves a later duplicate prompt in the queue.
    const idx = entryId
      ? list.findIndex(q => q.id === entryId)
      : list.findIndex(q => q.text.trim() === text.trim())
    if (idx === -1) return
    list.splice(idx, 1)
    if (!list.length) delete queuedMessages.value[chatId]
  }

  function reorderQueued(chatId: string, fromIndex: number, toIndex: number) {
    const list = queuedMessages.value[chatId]
    if (!list || fromIndex < 0 || fromIndex >= list.length) return
    toIndex = Math.max(0, Math.min(toIndex, list.length - 1))
    if (fromIndex === toIndex) return
    const [moved] = list.splice(fromIndex, 1)
    list.splice(toIndex, 0, moved)
    queuedMessages.value[chatId] = [...list]
    const ws = sockets.value[chatId]
    if (ws?.readyState === WebSocket.OPEN && moved?.id) {
      const beforeId = list[toIndex + 1]?.id || null
      ws.send(JSON.stringify({ type: 'queue_reorder', entry_id: moved.id, before_id: beforeId }))
    }
  }

  function editQueued(chatId: string, entryId: string, text: string, images?: string[]) {
    const list = queuedMessages.value[chatId]
    if (!list) return false
    const entry = list.find(q => q.id === entryId)
    if (!entry) return false
    entry.text = text
    entry.images = images
    const ws = sockets.value[chatId]
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'queue_edit', entry_id: entryId, text, images }))
    }
    return true
  }

  function clearQueued(chatId: string) {
    delete queuedMessages.value[chatId]
  }

  function stopChat(chatId: string) {
    const ws = sockets.value[chatId]
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'stop' }))
      return
    }
    // The socket can be mid-reconnect (liveness watchdog, a network blip)
    // right when the user clicks Stop, silently dropping the WS message with
    // no way to retry from here. Fall back to the HTTP route so Stop always
    // reaches the server even while the socket is unusable.
    void api.post(`/api/chats/${chatId}/stop`, {})
  }

  function respondPermission(
    chatId: string,
    requestId: string,
    approved: boolean,
    reason = '',
  ) {
    // Pop the bubble optimistically so rapid-tapping the same button
    // doesn't double-send. If the WS is dead, the server resolves its
    // pending future on disconnect via `cancel_all`.
    const list = pendingPermissions.value[chatId]
    if (list) {
      const next = list.filter(p => p.request_id !== requestId)
      if (next.length) {
        pendingPermissions.value[chatId] = next
      } else {
        delete pendingPermissions.value[chatId]
        delete activeQuestions.value[chatId]
      }
    }
    // Clear the persisted attention flag optimistically too, so a
    // GET /api/chats refresh that lands before the server's own clear
    // round-trips doesn't resurrect the card via rebuildPendingPermission.
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (chat?.pending_permission) chat.pending_permission = ''
    const ws = sockets.value[chatId]
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(
        JSON.stringify({
          type: 'permission_response',
          request_id: requestId,
          approved,
          reason,
        }),
      )
    }
  }

  function respondQuestion(
    chatId: string,
    requestId: string,
    answers: Record<string, string[]>,
  ) {
    markResolvedQuestion(chatId)
    delete activeQuestions.value[chatId]
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (chat?.pending_question) chat.pending_question = ''
    const ws = sockets.value[chatId]
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'question_response',
        request_id: requestId,
        answers,
      }))
    }
  }

  function respondCapability(
    chatId: string,
    requestId: string,
    action: 'switch' | 'picker' | 'cancel',
    modelId = '',
  ) {
    // Pop the card optimistically so rapid taps don't double-send. The
    // server resolves its pending future on timeout/disconnect regardless.
    const list = activeCapabilityQuestions.value[chatId]
    if (list) {
      const next = list.filter(q => q.request_id !== requestId)
      if (next.length) {
        activeCapabilityQuestions.value[chatId] = next
      } else {
        delete activeCapabilityQuestions.value[chatId]
      }
    }
    const ws = sockets.value[chatId]
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'capability_response',
        request_id: requestId,
        action,
        model_id: modelId,
      }))
    }
  }

  // ── Voice ───────────────────────────────────────────────────────────

  async function transcribeVoice(chatId: string, audioBlob: Blob): Promise<string> {
    const form = new FormData()
    // Name the part after what the blob actually is: the server derives the
    // saved file's extension from it, and on-device dictation can only read
    // the containers CoreAudio understands (wav, m4a), not WebM.
    const ext = audioBlob.type.includes('wav') ? 'wav'
      : audioBlob.type.includes('mp4') || audioBlob.type.includes('m4a') ? 'm4a'
        : audioBlob.type.includes('ogg') ? 'ogg'
          : 'webm'
    form.append('audio', audioBlob, `voice.${ext}`)
    const res = await fetch(`/api/chats/${chatId}/voice`, {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }))
      throw new Error(err.error || `Voice failed: ${res.status}`)
    }
    const data: VoiceResult = await res.json()
    return data.text
  }

  async function speakMessage(chatId: string, text: string): Promise<Blob> {
    const res = await fetch(`/api/chats/${chatId}/speak`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
      credentials: 'same-origin',
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ error: res.statusText }))
      throw new Error(err.error || `Speech failed: ${res.status}`)
    }
    return res.blob()
  }

  // ── Images ──────────────────────────────────────────────────────────

  async function uploadImages(chatId: string, files: File[]): Promise<string[]> {
    const refs = await uploadImageRefs(chatId, files)
    addPendingImageRefs(chatId, refs)
    return refs
  }

  function addPendingImageRefs(chatId: string, refs: string[]): void {
    if (!refs.length) return
    const existing = getPendingBucket(pendingImagesByChat.value, chatId)
    setPendingBucket(pendingImagesByChat.value, chatId, [...existing, ...refs])
    persistPendingImages()
  }

  async function uploadImageRefs(chatId: string, files: File[]): Promise<string[]> {
    const form = new FormData()
    for (const f of files) {
      form.append(f.name, f)
    }
    const res = await fetch(`/api/chats/${chatId}/images`, {
      method: 'POST',
      body: form,
      credentials: 'same-origin',
    })
    if (!res.ok) throw new Error('Image upload failed')
    const results: { ref?: string; error?: string }[] = await res.json()
    return results.filter(r => r.ref).map(r => r.ref!)
  }

  function removePendingImage(index: number) {
    if (!activeChatId.value) return
    const next = pendingImages.value.filter((_, i) => i !== index)
    setPendingBucket(pendingImagesByChat.value, activeChatId.value, next)
    persistPendingImages()
  }

  function clearPendingImages() {
    if (!activeChatId.value) return
    setPendingBucket<string>(pendingImagesByChat.value, activeChatId.value, [])
    persistPendingImages()
  }

  // ── Pending file comments ──────────────────────────────────────────
  // Captured by the markdown viewer when the user highlights text and adds a
  // note. Sent on the next message in the active chat. UUID generation falls
  // back to a Math.random id if crypto.randomUUID is unavailable (older WebView).
  function addPendingComment(c: {
    path: string
    selection: string
    comment: string
    lineStart?: number | null
    lineEnd?: number | null
    colIndex?: number | null
    colHeader?: string | null
    images?: string[]
  }): string {
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as { randomUUID: () => string }).randomUUID()
      : `c_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    const entry: PendingComment = {
      id,
      path: c.path,
      selection: c.selection,
      comment: c.comment,
      lineStart: c.lineStart ?? null,
      lineEnd: c.lineEnd ?? c.lineStart ?? null,
      colIndex: c.colIndex ?? null,
      colHeader: c.colHeader ?? null,
      images: c.images,
    }
    if (activeChatId.value) {
      const existing = getPendingBucket(pendingCommentsByChat.value, activeChatId.value)
      setPendingBucket(pendingCommentsByChat.value, activeChatId.value, [...existing, entry])
      persistPendingComments()
    }
    // Also persist into the durable per-file store so the comment stays visible
    // in the document viewer after it is sent.
    const list = fileComments.value[c.path] || []
    if (!list.some(x => x.id === id)) {
      fileComments.value[c.path] = [...list, { ...entry, createdAt: new Date().toISOString() }]
      persistFileComments()
    }
    return id
  }
  function removePendingComment(id: string): void {
    pendingComments.value = pendingComments.value.filter(c => c.id !== id)
    persistPendingComments()
  }
  function clearPendingComments(): void {
    pendingComments.value = []
    persistPendingComments()
  }

  // ── Durable file comments ──────────────────────────────────────────
  function fileCommentsFor(path: string): FileComment[] {
    return fileComments.value[path] || []
  }
  function removeFileComment(path: string, id: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const next = list.filter(c => c.id !== id)
    if (next.length) fileComments.value[path] = next
    else delete fileComments.value[path]
    // Also drop from pending if it hasn't been sent yet.
    pendingComments.value = pendingComments.value.filter(c => c.id !== id)
    persistFileComments()
    persistPendingComments()
  }

  function updateFileComment(path: string, id: string, comment: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const next = list.map(c => c.id === id ? { ...c, comment } : c)
    fileComments.value[path] = next
    // Also update pending if it hasn't been sent yet.
    pendingComments.value = pendingComments.value.map(c =>
      c.id === id ? { ...c, comment } : c
    )
    persistFileComments()
    persistPendingComments()
  }

  // ── Pinned file viewer (per chat/project) ──────────────────────────
  function pinFile(id: string, path: string): void {
    pinnedFilePaths.value = { ...pinnedFilePaths.value, [id]: path }
    // Pinning a file the user had closed clears that path's dismissal, so a
    // later surface of it is allowed to reopen it again.
    const dismissed = dismissedAutoPins.value[id]
    if (dismissed?.includes(path)) {
      const remaining = dismissed.filter(p => p !== path)
      const nextDismissed = { ...dismissedAutoPins.value }
      if (remaining.length) nextDismissed[id] = remaining
      else delete nextDismissed[id]
      dismissedAutoPins.value = nextDismissed
      persistDismissedAutoPins()
    }
    persistPinnedFiles()
  }
  function unpinFile(id: string): void {
    const next = { ...pinnedFilePaths.value }
    const closedPath = next[id]
    delete next[id]
    pinnedFilePaths.value = next
    if (closedPath) {
      const dismissed = dismissedAutoPins.value[id] || []
      if (!dismissed.includes(closedPath)) {
        dismissedAutoPins.value = {
          ...dismissedAutoPins.value,
          [id]: [...dismissed, closedPath],
        }
        persistDismissedAutoPins()
      }
    }
    persistPinnedFiles()
  }
  function pinnedFileFor(id: string): string | undefined {
    return pinnedFilePaths.value[id]
  }

  // ── Pending chat comments ─────────────────────────────────────────
  function addPendingChatComment(c: Omit<PendingChatComment, 'id'>): string {
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as { randomUUID: () => string }).randomUUID()
      : `cc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    if (activeChatId.value) {
      const existing = getPendingBucket(pendingChatCommentsByChat.value, activeChatId.value)
      setPendingBucket(pendingChatCommentsByChat.value, activeChatId.value, [
        ...existing,
        { id, ...c },
      ])
      persistPendingChatComments()
    }
    return id
  }
  function removePendingChatComment(id: string): void {
    pendingChatComments.value = pendingChatComments.value.filter(c => c.id !== id)
    persistPendingChatComments()
  }
  function clearPendingChatComments(): void {
    pendingChatComments.value = []
    persistPendingChatComments()
  }
  function updatePendingChatComment(id: string, comment: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], comment })
    persistPendingChatComments()
  }
  function addPendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    if (!existing.includes(imageRef)) {
      setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], images: [...existing, imageRef] })
      persistPendingChatComments()
    }
  }
  function removePendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    const next = existing.filter(img => img !== imageRef)
    setListIndex(pendingChatComments.value, idx, { ...pendingChatComments.value[idx], images: next.length ? next : undefined })
    persistPendingChatComments()
  }
  function addFileCommentImage(path: string, id: string, imageRef: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const idx = list.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = list[idx].images || []
    if (!existing.includes(imageRef)) {
      const next = [...list]
      next[idx] = { ...next[idx], images: [...existing, imageRef] }
      fileComments.value[path] = next
      // Sync to pending if it exists there
      const pIdx = pendingComments.value.findIndex(c => c.id === id)
      if (pIdx !== -1) {
        setListIndex(pendingComments.value, pIdx, { ...pendingComments.value[pIdx], images: [...existing, imageRef] })
        persistPendingComments()
      }
      persistFileComments()
    }
  }
  function removeFileCommentImage(path: string, id: string, imageRef: string): void {
    const list = fileComments.value[path]
    if (!list) return
    const idx = list.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = list[idx].images || []
    const nextImages = existing.filter(img => img !== imageRef)
    const next = [...list]
    next[idx] = { ...next[idx], images: nextImages.length ? nextImages : undefined }
    fileComments.value[path] = next
    const pIdx = pendingComments.value.findIndex(c => c.id === id)
    if (pIdx !== -1) {
      setListIndex(pendingComments.value, pIdx, { ...pendingComments.value[pIdx], images: nextImages.length ? nextImages : undefined })
      persistPendingComments()
    }
    persistFileComments()
  }

  // ── Event handling ──────────────────────────────────────────────────

  /** Currently accumulated timeline entries for the active turn (tools + intermediate text). */
  const currentTimeline = computed<StreamEntry[]>(() => streamingTimeline.value[activeChatId.value || ''] || [])
  /** Live token totals for the active turn, or null when none reported yet. */
  const currentLiveUsage = computed<{ input: number; output: number } | null>(
    () => liveUsage.value[activeChatId.value || ''] || null
  )
  /** Epoch millis when the active turn started streaming, or 0 if unknown. */
  const currentStreamStartedAt = computed<number>(
    () => streamStartedAt.value[activeChatId.value || ''] || 0
  )
  /** Legacy: flat list of just the tool lines (for callers that only care about tool activity). */
  const currentActivity = computed(() => {
    const lines: string[] = []
    for (const e of currentTimeline.value) {
      if (e.kind === 'tool') lines.push(...e.content.split('\n'))
    }
    return lines
  })

  function _commitStreamingTextToTimeline(chatId: string) {
    const text = (streamingText.value[chatId] || '').trim()
    const phase = streamingTextPhase.value[chatId]
    if (!text) {
      delete streamingTextPhase.value[chatId]
      return
    }
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    streamingTimeline.value[chatId].push({ kind: 'text', content: text, phase })
    streamingText.value[chatId] = ''
    delete streamingTextPhase.value[chatId]
  }

  function _commitStreamingThinkingToTimeline(chatId: string) {
    const text = (streamingThinking.value[chatId] || '').trim()
    if (!text) return
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    streamingTimeline.value[chatId].push({ kind: 'thinking', content: text })
    streamingThinking.value[chatId] = ''
  }

  function _pushToolLine(chatId: string, line: string) {
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    const arr = streamingTimeline.value[chatId]
    const last = arr[arr.length - 1]
    if (last && last.kind === 'tool') {
      // Dedupe within the same tool block: replace if same tool name with more detail; append otherwise.
      const lastLines = last.content.split('\n')
      const lastLine = lastLines[lastLines.length - 1]
      const toolName = line.split(' ')[1]
      if (lastLine && lastLine.split(' ')[1] === toolName && line.length > lastLine.length) {
        lastLines[lastLines.length - 1] = line
        last.content = lastLines.join('\n')
      } else if (lastLine !== line) {
        last.content = last.content + '\n' + line
      }
    } else {
      arr.push({ kind: 'tool', content: line })
    }
  }

  function _pushStatusLine(chatId: string, text: string) {
    // Compaction (and similar) status ticks arrive repeatedly while a turn
    // works — fold them into one live line in the trace instead of stacking
    // a new bubble per tick.
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    const arr = streamingTimeline.value[chatId]
    const last = arr[arr.length - 1]
    if (last && last.kind === 'status') {
      last.content = text
    } else {
      arr.push({ kind: 'status', content: text })
    }
  }

  function _pushFileCard(
    chatId: string,
    payload: { file_path: string; action: string; tool: string; tool_use_id?: string },
  ) {
    // Ignore shell false positives ("There") that are not real paths.
    if (!isPlausibleFilePath(payload.file_path)) return
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    streamingTimeline.value[chatId].push({
      kind: 'filecard',
      content: payload.file_path,
      file_path: payload.file_path,
      action: payload.action,
      tool: payload.tool,
      tool_use_id: payload.tool_use_id,
    })
  }

  // Show a file the agent deliberately surfaced via the `file_surface` MCP
  // tool (action === 'surfaced'). Ordinary Write/Edit touches only ever get an
  // inline card: this used to be guessed at by extension (.md/.csv) plus a
  // bookkeeping skip-list, which both missed real deliverables and fired on
  // noisy writes. An explicit tool call is a genuine signal; an extension is not.
  //
  // Because the call is explicit, it outranks whatever is currently pinned and
  // replaces it. The only thing it respects is a dismissal of the *same* path
  // (see dismissedAutoPins): the user closed that file, and a WS reconnect
  // replaying the stream buffer must not shove it back. On a narrow viewport
  // there is no split panel, so open the viewer modal instead of dropping the
  // request on the floor. localStorage-backed like every other pin.
  function _applySurfaceRequests(
    chatId: string,
    touches: Array<{ file_path?: string; action?: string }>,
  ): void {
    if (typeof window === 'undefined') return
    // Freshest surfaced artifact wins (last touch in the batch).
    for (let i = touches.length - 1; i >= 0; i--) {
      const touch = touches[i]
      if (touch?.action !== 'surfaced') continue
      const raw = touch.file_path
      if (!raw || !isPlausibleFilePath(raw)) continue
      if (dismissedAutoPins.value[chatId]?.includes(raw)) return
      if (pinnedFileFor(chatId) === raw) return
      if (window.innerWidth <= 768) {
        _openSurfacedInViewer(raw, chatId)
        return
      }
      pinFile(chatId, raw)
      return
    }
  }

  // Mobile fallback for an explicit surface. Never interrupts: an already-open
  // viewer (the user may be mid-edit there) keeps whatever it is showing, and
  // the inline file card stays as the way in.
  function _openSurfacedInViewer(path: string, chatId: string): void {
    try {
      const viewer = useFileViewerStore()
      if (viewer.isOpen) return
      void viewer.open(path, null, chatId)
    } catch { /* store unavailable outside an app context */ }
  }

  function _flushTimeline(chatId: string): StreamEntry[] {
    const entries = streamingTimeline.value[chatId] || []
    streamingTimeline.value[chatId] = []
    return entries
  }

  function beginHostReconnect(chatId: string, chatMessages: ChatMessage[]) {
    hostConnectionUnavailable.value = true
    _flushTimeline(chatId)
    // Also clean repeated proxy errors already painted by an older frontend
    // before this structured event arrived during a rolling deploy.
    messages.value[chatId] = normalizeMessages([...chatMessages])
    streaming.value[chatId] = false
    streamingText.value[chatId] = ''
    streamingThinking.value[chatId] = ''
    delete streamingTextPhase.value[chatId]
    delete liveUsage.value[chatId]
    delete streamStartedAt.value[chatId]
    persistStreamStartedAt()
    delete pendingPermissions.value[chatId]
  }

  function handleEvent(chatId: string, event: WsEvent) {
    const msgs = messages.value[chatId] || []

    // A summary belongs only to the moment the user re-enters a quiet chat.
    // `queued` always represents new user activity (a new prompt is now
    // waiting), so it always invalidates the summary.
    //
    // `user_echo` and `result` are handled inside their switch cases below:
    // the broker replays them on every WS reconnect, and a no-op replay
    // (turn already rendered, or no final text on a result) must NOT clear
    // the summary. The user opens a chat, scrolls to re-orient, and the
    // summary disappearing on a broker replay is the wrong behavior.
    if (event.type === 'queued') {
      clearReentrySummary(chatId)
    }

    // Any event that implies an in-flight stream flips the flag, so a resumed
    // stream (WS reconnect with buffered-event replay from the server broker)
    // renders as "streaming" without the client having called sendMessage.
    // `user_echo` is included so a fresh subscribe that only has the echo
    // buffered (turn just started, no deltas yet) still shows the indicator.
    // `model_changed` is intentionally omitted: it is emitted after a
    // successful capability fallback's terminal `result`, so including it
    // here would flip streaming back on after the turn already ended.
    const streamingEventTypes = new Set(['text_delta', 'tool_use', 'thinking', 'status', 'user_echo', 'token_usage'])
    if (streamingEventTypes.has(event.type) && !streaming.value[chatId]) {
      streaming.value[chatId] = true
      if (streamingText.value[chatId] === undefined) streamingText.value[chatId] = ''
      if (streamingThinking.value[chatId] === undefined) streamingThinking.value[chatId] = ''
    }
    // Anchor the live elapsed timer the first time we see this turn stream.
    // On a WS reconnect mid-turn we don't know the true start, so this is a
    // lower bound (timer resumes from now); the final duration on the result
    // bubble remains authoritative.
    if (streamingEventTypes.has(event.type) && !streamStartedAt.value[chatId]) {
      streamStartedAt.value[chatId] = Date.now()
      persistStreamStartedAt()
    }

    switch (event.type) {
      case 'user_echo': {
        // Broker echoes the user prompt first, so a reconnecting client can
        // render the user turn without depending on /messages being ready.
        const trimmed = (event.text || '').trim()
        if (!trimmed) break
        // Queued follow-ups are drained as individual turns. Remove only the
        // entry that just started so later messages remain visible/editable.
        removeEchoedQueued(chatId, event.entry_id, trimmed)
        // The server received and started the send: drop the recovery copy.
        acknowledgeSend(chatId, trimmed)
        const turnIndex = event.turn_index
        // Dedup by server-assigned turn_index when available. Covers the
        // mid-stream reload case: /messages hydrates user bubbles with their
        // turn_index, so the replayed user_echo for the same turn is a no-op
        // regardless of what else is in the tail.
        if (turnIndex != null) {
          const existingWithTurn = msgs.find(
            m => m.role === 'user' && m.turn_index === turnIndex,
          )
          if (existingWithTurn) {
            // Already rendered (either from loadMessages on reload or from a
            // previous receipt of the same echo). Don't push a duplicate, but
            // do reflect the implied streaming state.
            if (event.unattended) existingWithTurn.unattended = true
            if (!streaming.value[chatId]) streaming.value[chatId] = true
            // This is a broker replay, not a new send: leave the re-entry
            // summary alone. The whole reason a user re-enters a chat is
            // orientation, and the summary must survive the WS-resume echo
            // storm until the user actually types or sends.
            break
          }
          // Look for an optimistic user message with matching content but no
          // assigned turn_index yet — reconcile it instead of pushing a
          // duplicate. A hydrated or already-echoed bubble always carries a
          // turn_index, so a user entry with turn_index == null is necessarily
          // an un-reconciled optimistic bubble we rendered at send time; that
          // invariant lets us scan the whole tail safely.
          //
          // Two shapes:
          //  - Fast path: the optimistic bubble is still the last thing in the
          //    tail (nothing streamed between send and echo). Upgrade it in
          //    place.
          //  - Stranded: the send was queued server-side behind a still-running
          //    turn, so that turn's assistant/activity blocks rendered before
          //    the echo arrived. The optimistic bubble now sits *above* those
          //    blocks. Drop the stale copy and fall through to push a fresh
          //    bubble at the tail, matching the server's turn order. The old
          //    "stop at the first assistant message" scan bailed here and left
          //    the bubble orphaned, rendering the turn twice.
          let upgraded = false
          let sawAssistant = false
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            if (m.role === 'user' && m.turn_index == null && m.content === trimmed) {
              if (sawAssistant) {
                msgs.splice(i, 1)
                break
              }
              m.turn_index = turnIndex
              if (event.unattended) m.unattended = true
              upgraded = true
              break
            }
            if (m.role === 'assistant') sawAssistant = true
          }
          if (upgraded) {
            if (!streaming.value[chatId]) streaming.value[chatId] = true
            break
          }
        } else {
          // Legacy path (older servers without turn_index): fall back to the
          // last-message content check.
          const last = msgs[msgs.length - 1]
          if (last && last.role === 'user' && last.content === trimmed) break
        }
        msgs.push({
          role: 'user',
          content: trimmed,
          timestamp: event.sent_at || new Date().toISOString(),
          images: event.images?.length ? event.images : undefined,
          turn_index: turnIndex,
          // Loop/schedule tick: marked so the bubble reads as self-driven
          // rather than something the user typed.
          unattended: event.unattended || undefined,
        })
        messages.value[chatId] = normalizeMessages([...msgs])
        // Flushed turn = we're streaming again. Make sure the flag reflects it.
        if (!streaming.value[chatId]) streaming.value[chatId] = true
        // Brand-new echo (not a replay) is the user actually starting a turn.
        // Clear the summary here so it disappears when the user sends, not on
        // an unrelated broker replay.
        clearReentrySummary(chatId)
        break
      }

      case 'queued': {
        // Server confirms the message was buffered. If we already pushed it
        // locally for optimistic rendering (matching id), skip. Otherwise (e.g.
        // another client queued it), add it so chips stay consistent. Older
        // servers may omit id, so fall back to content matching.
        const trimmed = (event.text || '').trim()
        if (!trimmed) break
        if (queuedTextAlreadyRendered(msgs, trimmed)) break
        const list = queuedMessages.value[chatId] || []
        const entryId = event.id || null
        if (entryId && list.some(q => q.id === entryId)) {
          // Already known; make sure text/images are in sync (defensive).
          const existing = list.find(q => q.id === entryId)
          if (existing) {
            existing.text = trimmed
            existing.images = event.images?.length ? event.images : undefined
          }
          break
        }
        if (!entryId && list.some(q => q.text === trimmed)) break
        list.push({
          id: entryId || makeQueuedId(),
          text: trimmed,
          images: event.images?.length ? event.images : undefined,
        })
        queuedMessages.value[chatId] = list
        break
      }

      case 'queue_state': {
        // Authoritative queue order from the backend (e.g. after a reorder/edit
        // from another client, or on reconnect). Rebuild local chips to match.
        const incoming = event.queue || []
        if (!incoming.length) {
          delete queuedMessages.value[chatId]
          break
        }
        queuedMessages.value[chatId] = incoming.map(q => ({
          id: q.id || makeQueuedId(),
          text: (q.text || '').trim(),
          images: q.images?.length ? q.images : undefined,
        }))
        break
      }

      case 'text_delta':
        // Visible text starts: any pending thinking block has ended, lock it
        // into the timeline so the Reasoning bubble renders it after the turn.
        _commitStreamingThinkingToTimeline(chatId)
        // Some providers start a new agent-message item when moving from progress
        // commentary to the terminal answer. Preserve that boundary instead
        // of concatenating both items into the final response buffer.
        if (
          event.phase
          && streamingText.value[chatId]
          && streamingTextPhase.value[chatId] !== event.phase
        ) {
          _commitStreamingTextToTimeline(chatId)
        }
        if (event.phase) streamingTextPhase.value[chatId] = event.phase
        streamingText.value[chatId] = (streamingText.value[chatId] || '') + event.text
        break

      case 'tool_use': {
        // AskUserQuestion is rendered as an interactive picker above the
        // composer, not as a trace line. The headless CLI auto-cancels the
        // call with empty answers; the user's next message implicitly
        // resolves the question. Parse the questions JSON the backend stuffs
        // into tool_input and stash it for the picker. Falls through to the
        // generic path on parse failure so the call still shows up in the
        // trace as a regular tool entry.
        if (event.tool_name === 'AskUserQuestion' && event.tool_input) {
          const qs = parseQuestions(event.tool_input, event.request_id || '')
          if (qs.length) {
            // A fresh live question supersedes any earlier resolved-picker
            // memory for this chat (keeps the set from growing and avoids a
            // reused native request id being wrongly suppressed).
            delete resolvedQuestions.value[chatId]
            activeQuestions.value[chatId] = qs
            // Nudge the user when the tab is backgrounded so they don't
            // miss a question that the model needs answered.
            if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
              const first = qs[0]
              pushToast({
                chat_id: chatId,
                title: 'ciaobot has a question',
                body: first?.question || first?.header || 'The model needs your input',
              })
            }
            break
          }
        }

        // Any pending streamed text or thinking becomes an intermediate
        // note in the timeline before the tool call fires.
        _commitStreamingThinkingToTimeline(chatId)
        _commitStreamingTextToTimeline(chatId)

        // File-mutating tool calls (Write/Edit/MultiEdit/NotebookEdit/Bash
        // creates) get their own inline preview card. Backend tags these with
        // `file_touch` / `file_touches` in chat_broker.event_to_json. Subagent
        // file writes also get a card, with the dispatch label preserved in
        // the `tool` field for context.
        const touches = event.file_touches?.length
          ? event.file_touches
          : (event.file_touch?.file_path ? [event.file_touch] : [])
        if (touches.length) {
          for (const touch of touches) {
            if (!touch?.file_path) continue
            _pushFileCard(chatId, {
              file_path: touch.file_path,
              action: touch.action || 'touched',
              tool: event.tool_name,
              tool_use_id: event.tool_use_id,
            })
          }
          _applySurfaceRequests(chatId, touches)
          break
        }

        // Tool calls that fire from inside a subagent arrive with
        // parent_tool_use_id set. They belong to the subagent, which the PWA
        // already renders in its own "Subagent activity" box (SubagentPanel,
        // fed by the subagent transcript). Inlining them in the parent trace
        // too double-counts the work and inflates the parent turn's tool-call
        // total (e.g. parent header shows "15 tool calls" while the box shows
        // "31"), so we drop them here and let the box own subagent activity.
        if (event.parent_tool_use_id) break

        const line = event.tool_input
          ? `${_toolIcon(event.tool_name)} ${event.tool_name} ${event.tool_input}`
          : `${_toolIcon(event.tool_name)} ${event.tool_name}`

        _pushToolLine(chatId, line)
        break
      }

      case 'tool_denied': {
        // The call was refused, so it never ran. Drop the file card it already
        // painted: the chip is emitted on request, which made a denied Write
        // look like a created file. Its activity line stays, so the attempt is
        // still visible in the trace.
        const timeline = streamingTimeline.value[chatId]
        if (timeline?.length && event.tool_use_id) {
          streamingTimeline.value[chatId] = timeline.filter(
            e => !(e.kind === 'filecard' && e.tool_use_id === event.tool_use_id),
          )
        }
        break
      }

      case 'thinking':
        // Thinking deltas fired from inside a Task subagent arrive with
        // parent_tool_use_id set. The subagent's transcript is rendered in its
        // own "Subagent activity" box (SubagentPanel), so accumulating these
        // deltas into the parent's thinking buffer would leak the subagent's
        // reasoning into the parent turn's trace and end up in the persisted
        // history as a stray _thinking message long after the subagent ended.
        if (event.parent_tool_use_id) break
        // Accumulate into the thinking buffer. Committed to the timeline
        // when the model switches to visible text or fires a tool_use
        // (those signal the end of this thinking block). For Anthropic
        // models thinking is usually short and the buffer flushes within
        // the same turn; for some models the
        // thinking block can be long and is the user's main view into
        // the model's actual reasoning, so dropping it would hurt.
        if (event.text) {
          streamingThinking.value[chatId] =
            (streamingThinking.value[chatId] || '') + event.text
        }
        break

      case 'status': {
        // Surface descriptive status notes (capability fallback "retrying
        // on …") as system messages. Ephemeral control tokens stay silent
        // so "thinking"/"stopped"/"requesting"/rate-limit markers do not
        // pollute history. Claude emits "requesting" while tools are pending;
        // those belong in the Activity trace via tool_use, not as chat lines.
        const message = (event.message || '').trim()
        const ephemeral = new Set(['thinking', 'stopped', 'requesting', 'rate_limit', 'model_rerouted'])
        // Drop rate-limit telemetry — allowance pings, warnings, and rejected ticks alike.
        // They are usage status for Settings, not conversation. A hard "Rate limit exceeded"
        // still shows as an error. Shared predicate mirrors the backend (see rateLimit.ts).
        const isTelemetry = isRateLimitTelemetry(message)
        // Compaction ticks repeat several times per pass. Unlike the
        // rate-limit pings they're useful operator signal, so fold them into
        // the live Thinking/Working trace (one line, updated in place)
        // instead of dropping them or stacking a chat bubble per tick.
        const isCompacting = /compact/i.test(message)
        if (isCompacting) {
          _pushStatusLine(chatId, message)
        } else if (message && !ephemeral.has(message) && !message.startsWith('error:') && !isTelemetry) {
          clearReentrySummary(chatId)
          msgs.push({
            role: 'system',
            content: message,
            timestamp: new Date().toISOString(),
          })
          messages.value[chatId] = normalizeMessages([...msgs])
          persistMessages()
        }
        break
      }

      case 'model_changed': {
        const chat = chats.value.find(c => c.chat_id === chatId)
        if (chat && event.model) {
          chat.model = event.model
        }
        break
      }

      case 'token_usage':
        // Cumulative, monotonic totals for the turn. Store the latest snapshot
        // so the live trace meta can show a running token count.
        liveUsage.value[chatId] = {
          input: event.input_tokens || 0,
          output: event.output_tokens || 0,
        }
        break

      case 'result': {
        // Final flush before the result event is materialized: lock any
        // trailing thinking/text deltas into the timeline so they render
        // in the correct order.
        _commitStreamingThinkingToTimeline(chatId)
        // A completed/interrupted turn may legitimately end after a
        // commentary item with no final answer. Keep that text in the trace;
        // never promote it into the response bubble via the defensive merge.
        //
        // The one exception is `fallback_final`: the provider already decided
        // this commentary IS the answer (a completed turn that emitted no
        // final_answer at all) and sent it as the result text. Committing it
        // to the trace too would render the same text twice, in Activity and
        // in the response bubble, so leave it for the result to carry.
        if (!event.fallback_final && streamingTextPhase.value[chatId] === 'commentary') {
          _commitStreamingTextToTimeline(chatId)
        }
        // Flush accumulated timeline preserving order: tool runs → _activity
        // system msgs, thinking → _thinking system msgs (rendered in the
        // Reasoning trace, never as the final answer), intermediate text →
        // assistant msgs. Matches how a reload from the server renders.
        const entries = _flushTimeline(chatId)
        // Defensive merge: the SDK's ResultEvent sometimes only captures the
        // first assistant text block in a tool loop, while post-tool text
        // deltas were already streamed into streamingText. Don't let a
        // partial event.text discard the rest.
        let text = (event.text || '').trim()
        const st = (streamingText.value[chatId] || '').trim()
        // Containment is checked whitespace-insensitively: the provider may
        // re-join the same streamed parts with different separators (opencode
        // joins text parts with a blank line, while the deltas were
        // concatenated raw), and that must not read as new content — it would
        // append both copies and render the answer twice.
        const squash = (s: string) => s.replace(/\s+/g, '')
        if (st && !squash(text).includes(squash(st))) {
          if (squash(st).includes(squash(text))) {
            text = st
          } else {
            text = text ? text + '\n\n' + st : st
          }
        }
        const now = new Date().toISOString()
        for (const entry of entries) {
          if (entry.kind === 'tool') {
            msgs.push({
              role: 'system',
              content: entry.content,
              timestamp: now,
              tool_name: '_activity',
            })
          } else if (entry.kind === 'filecard') {
            msgs.push({
              role: 'system',
              content: entry.file_path,
              timestamp: now,
              tool_name: '_filecard',
              file_path: entry.file_path,
              action: entry.action,
              tool: entry.tool,
            })
          } else if (entry.kind === 'thinking') {
            msgs.push({
              role: 'system',
              content: entry.content,
              timestamp: now,
              tool_name: '_thinking',
            })
          } else if (entry.kind === 'status') {
            // Transient trace-only line (e.g. compaction ticks) — never
            // persisted as a chat message, live view only.
            continue
          } else {
            // Skip timeline text entries that are already represented in the
            // final merged text so the trace doesn't duplicate the answer bubble.
            const entryText = entry.content.trim()
            if (
              entry.phase !== 'commentary'
              && text
              && entryText
              && text.indexOf(entryText) >= 0
            ) continue
            msgs.push({
              role: 'assistant',
              content: entry.content,
              timestamp: now,
              phase: entry.phase,
            })
          }
        }
        if (event.session_id) {
          const chat = chats.value.find(c => c.chat_id === chatId)
          if (chat) chat.session_id = event.session_id
        }
        // Clear the re-entry summary only when the result represents a turn
        // that just finished while the user was watching. If `streaming` was
        // already false, this is a broker replay for a turn the user has
        // already been reading and the summary still applies. The summary
        // also clears at user send (sendMessage / fresh user_echo).
        const wasStreaming = streaming.value[chatId] === true
        if (text.trim() || event.is_error) {
          msgs.push({
            role: 'assistant',
            content: text.trim(),
            timestamp: event.completed_at || new Date().toISOString(),
            is_error: event.is_error,
            effective_model: event.effective_model,
            usage: event.usage,
            quota: event.quota,
            duration_ms: event.duration_ms,
            phase: 'final_answer',
          })
          const isActive = activeChatId.value === chatId &&
            (typeof document === 'undefined' || document.visibilityState === 'visible')
          if (!isActive) {
            unread.value[chatId] = 1
            persistUnread()
          }
        }
        messages.value[chatId] = normalizeMessages([...msgs])
        streaming.value[chatId] = false
        streamingText.value[chatId] = ''
        streamingThinking.value[chatId] = ''
        delete streamingTextPhase.value[chatId]
        delete liveUsage.value[chatId]
        delete streamStartedAt.value[chatId]
        persistStreamStartedAt()
        // Turn ended: the server has already resolved any still-pending gate
        // futures as deny via cancel_all(). Drop the bubbles on our side too
        // so a late click can't race a brand-new turn.
        delete pendingPermissions.value[chatId]
        persistMessages()
        if (wasStreaming) {
          // The result closed a turn that was actually in flight on this
          // client. The re-entry summary no longer reflects the chat
          // state, so drop it. Skipped on a broker replay (wasStreaming
          // false) so a scroll-induced resume doesn't dismiss the summary
          // for a turn the user is still re-reading.
          clearReentrySummary(chatId)
        }
        // Reconcile with the authoritative SDK session. Handles the reconnect
        // case where /messages already had this turn (dedups) and the race
        // where the SDK session file lags the result event (retries until the
        // final bubble is visible).
        void reconcileAfterResult(chatId)
        break
      }

      case 'chat_retry': {
        const chat = chats.value.find(c => c.chat_id === chatId)
        if (chat) {
          chat.retry = event.status ? {
            status: event.status,
            next_at: event.next_at || '',
            last_error: event.last_error || '',
            attempts: event.attempts || 0,
            interval_seconds: event.interval_seconds || 3600,
          } : null
        }
        break
      }

      case 'error': {
        if (isRestartDrainMessage(event.message)) {
          undoOptimisticSend(chatId)
          beginServerRestart(event.message)
          break
        }
        // Rolling-upgrade compatibility: old client proxies emitted this as a
        // generic error string. Treat it as the structured connection state.
        if (isHostConnectionUnavailableMessage(event.message)) {
          beginHostReconnect(chatId, msgs)
          break
        }
        _flushTimeline(chatId)
        msgs.push({
          role: 'system',
          content: `Error: ${event.message}`,
          timestamp: new Date().toISOString(),
        })
        messages.value[chatId] = normalizeMessages([...msgs])
        streaming.value[chatId] = false
        streamingText.value[chatId] = ''
        streamingThinking.value[chatId] = ''
        delete streamingTextPhase.value[chatId]
        delete liveUsage.value[chatId]
        delete streamStartedAt.value[chatId]
        persistStreamStartedAt()
        delete pendingPermissions.value[chatId]
        persistMessages()
        break
      }

      case 'host_unreachable': {
        beginHostReconnect(chatId, msgs)
        break
      }

      case 'server_restarting': {
        undoOptimisticSend(chatId)
        beginServerRestart(event.message)
        break
      }

      case 'permission_request': {
        // Auto mode classifier escalated: model wants to run a tool, pop the
        // Approve/Deny bubble. Keep a visible timeline line too so the user
        // sees the context even if they dismiss the buttons by scrolling.
        _commitStreamingTextToTimeline(chatId)
        _pushToolLine(chatId, `\u{1F6A7} Permission: ${event.tool_name} - ${event.message}`)
        const list = pendingPermissions.value[chatId] || []
        // Dedup by request_id in case the server replays it on reconnect.
        if (!list.some(p => p.request_id === event.request_id)) {
          pendingPermissions.value[chatId] = [
            ...list,
            {
              request_id: event.request_id,
              tool_name: event.tool_name,
              tool_input: event.tool_input || '',
              message: event.message,
              received_at: Date.now(),
            },
          ]
        }
        // If the window is backgrounded, nudge the user via an in-app toast.
        // The server ships a push notification too (routed separately through
        // the service-worker); this toast covers the tab-visible case.
        if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
          pushToast({
            chat_id: chatId,
            title: 'ciaobot needs approval',
            body: `${event.tool_name}: ${event.message}`,
          })
        }
        break
      }

      case 'model_capability_question': {
        // The selected model cannot see the attached images; the engine is
        // holding the turn until the user picks a vision-capable model,
        // opens the full picker, or cancels. Keep the card per request_id
        // (a reconnect replay must not duplicate it).
        const list = activeCapabilityQuestions.value[chatId] || []
        if (!list.some(q => q.request_id === event.request_id)) {
          activeCapabilityQuestions.value[chatId] = [
            ...list,
            parseCapabilityQuestion(event),
          ]
        }
        break
      }

      case 'chat_title': {
        const chat = chats.value.find(c => c.chat_id === event.chat_id)
        if (chat) chat.title = event.title
        break
      }
    }
  }

  function _toolIcon(name: string): string {
    const icons: Record<string, string> = {
      Read: '\u{1F4D6}',     // 📖
      Edit: '\u270F\uFE0F',   // ✏️
      Write: '\u{1F4DD}',    // 📝
      Bash: '$',
      Grep: '\u{1F50D}',     // 🔍
      Glob: '\u{1F4C2}',     // 📂
      Agent: '\u{1F916}',    // 🤖
      Skill: '\u26A1',       // ⚡
      WebSearch: '\u{1F310}', // 🌐
      WebFetch: '\u{1F310}',  // 🌐
      TaskCreate: '\u2611\uFE0F', // ☑️
      TaskUpdate: '\u2611\uFE0F', // ☑️
    }
    return icons[name] || '\u2699\uFE0F' // ⚙️
  }

  restoreState()
  restoreUnread()

  return {
    // State
    projects, chats, workspaces, workspaceProviderOptions, workspaceAppDefaultModel, activeWorkspace, activeChatId, bootstrapped, messages, messageHistoryLoading, subagents, unread, reentrySummaries,
    streaming, streamingText, streamingThinking, pendingImages, pendingComments, pendingChatComments, fileComments, queuedMessages,
    projectStreaming, backgroundAgents, toasts, pendingPermissions, activeQuestions, activeCapabilityQuestions, creatingChatProjectIds,
    serverRestarting, serverRestartMessage, hostConnectionUnavailable,
    // Computed
    workspaceProjects, workspaceOptions, activeChat, activeProject, activeMessages, activeSubagents,
    isStreaming, currentStreamingText, currentStreamingThinking, currentQueued, activeBackgroundAgents, currentActivity, currentTimeline, currentLiveUsage, currentStreamStartedAt, projectChats, projectChatRows, projectChatGroups,
    chatUnread, chatNeedsInput, chatPendingQuestion, projectNeedsInput, projectUnread, workspaceUnread, workspaceNeedsInput, totalUnread, attentionChatCount, clearUnread, markRead, markAllRead,
    recentChats, activeChatsAll, activeDelegatesFor, projectIsStreaming, isChatStreaming, chatHasBackgroundAgents, chatHasActiveDelegates, workspaceIsStreaming, projectFor,
    chatPostprocess, chatIsPostprocessing, postprocessingChats, workspacePostprocessingCount, projectPostprocessingCount,
    insightsFailedChats, workspaceInsightsFailedCount,
    archivingChats, isArchiving, archivingChatsList, workspaceArchivingCount, projectArchivingCount,
    // Actions
    fetchAll, fetchWorkspaces, createWorkspace, updateWorkspace, deleteWorkspace,
    createProject, updateProject, reorderProjects, deleteProject, completeProject,
    fetchCompletedProjects, restoreProject,
    createChat, newChatInGeneral, renameChat, updateChat, handoverChat, forkChat, moveChat, deleteChat, closeChat, requestReentrySummary, requestReentrySummaryIfUseful, archiveChat, continueArchivedChat, newSession,
    setChatRetry, stopChatRetry, tryChatRetryNow, retryInsights,
    switchChat, switchWorkspace, openChatFromDeepLink, ensureWorkspaceForChat,
    syncLatest,
    sendMessage, stopChat, respondPermission, respondQuestion, respondCapability, markResolvedQuestion, transcribeVoice, speakMessage, uploadImages, uploadImageRefs, addPendingImageRefs, removePendingImage, clearPendingImages,
    addPendingComment, removePendingComment, clearPendingComments,
    addPendingChatComment, removePendingChatComment, clearPendingChatComments, updatePendingChatComment,
    addPendingChatCommentImage, removePendingChatCommentImage,
    addFileCommentImage, removeFileCommentImage,
    fileCommentsFor, removeFileComment, updateFileComment,
    pinFile, unpinFile, pinnedFileFor,
    removeQueued, removeQueuedById, reorderQueued, editQueued, clearQueued,
    loadMessages, loadSubagents, setReentrySummaryEnabled,
    canLoadOlder, isLoadingOlder, loadOlderMessages, expandMessagePart,
    connectWs, disconnectWs, connectEventsWs,
    beginServerRestart, restoreState,
    pushToast, pushErrorToast, dismissToast, fixError, restoreDraft,
    packageStatus, checkPackageStatus,
  }
})
