import { defineStore } from 'pinia'
import { ref, computed, onScopeDispose, watch, toRaw } from 'vue'
import { api } from '../lib/api'
import { buildFixPrompt } from '../lib/fixError'
import { isPlausibleFilePath } from '../lib/filePaths'
import { useFileViewerStore } from './fileViewer'
import { isRateLimitTelemetry } from '../lib/rateLimit'
import {
  isRestartDrainMessage,
  reloadWhenServerReady,
  restartMessageForDisplay,
} from '../lib/serverRestart'
import { errorMessage } from '../lib/errorMessage'
import { clearChatDraft, readChatDraft, readOrphanCandidates, writeChatDraft } from '../lib/chatDrafts'
import { isPostprocessing, postprocessNeedsRetry } from '../lib/postprocessView'
import type {
  ArchiveChatResponse,
  ArchivedWorkspace,
  ArchivedWorkspacesResponse,
  ArchiveJobView,
  ProjectInfo,
  ChatInfo,
  ChatPostprocess,
  ChatMessage,
  RunningSubagent,
  RunningSubagentsResponse,
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
import { bareAgentId, sameAgent } from '../lib/subagentIds'
import {
  chatWsReconnectDelayMs,
  isHostConnectionUnavailableMessage,
  isHostPolicyMessage,
  isTerminalWsClose,
  isWsAuthClose,
  isWsPolicyClose,
  shouldReconnectActiveChatOnStreamingStarted,
} from '../lib/chatWs'
import {
  dropSupersededLiveTail,
  historySignature,
  isLiveTraceRow,
  mergeMessageFields,
  mergeMetadata,
  normalizeMessages,
  queuedTextAlreadyRendered,
  toChatMessage,
  toolIcon,
  type ServerRow,
} from '../lib/chatHistory'
import {
  parseCapabilityQuestion,
  parseQuestions,
  questionsSignature,
  type ActiveQuestion,
  type CapabilityQuestion,
} from '../lib/chatQuestions'
import { createChatAnnotations, type PreparedMessage } from './chatAnnotations'

// Moved to focused modules; re-exported so importers of this store keep
// resolving them. `lib/chatWs.ts` owns the reconnect policy and
// `lib/safeList.ts` the checked list write.
export {
  chatWsReconnectDelayMs,
  isHostConnectionUnavailableMessage,
  isHostPolicyMessage,
  isTerminalWsClose,
  isWsAuthClose,
  isWsPolicyClose,
  shouldReconnectActiveChatOnStreamingStarted,
}
export { setListIndex } from '../lib/safeList'

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
  const activeWorkspace = ref<WorkspaceName>('personal')
  const activeChatId = ref<string | null>(null)
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
  // Live subagents per chat, from /api/subagents/running. Metadata only (no
  // transcripts), refreshed on a poll while anything is working, so the
  // sidebar can list what every chat has in flight — not just the open one.
  const runningSubagents = ref<Record<string, RunningSubagent[]>>({})
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
  // Everything staged against the next message (pending images, pending file
  // and chat comments), plus durable per-file comments, pinned files and
  // auto-pin dismissals, lives in `stores/chatAnnotations.ts` together with the
  // localStorage keys that back them. The refs below ARE that module's refs —
  // it is a plain factory, not a second Pinia store — so the names this store
  // returns behave exactly as when they were declared here.
  const annotations = createChatAnnotations({ activeChatId })
  const {
    pendingImages,
    pendingComments,
    pendingChatComments,
    fileComments,
    addPendingImageRefs,
    removePendingImage,
    clearPendingImages,
    addPendingComment,
    removePendingComment,
    clearPendingComments,
    fileCommentsFor,
    removeFileComment,
    updateFileComment,
    pinFile,
    unpinFile,
    pinnedFileFor,
    addPendingChatComment,
    removePendingChatComment,
    clearPendingChatComments,
    updatePendingChatComment,
    addPendingChatCommentImage,
    removePendingChatCommentImage,
    addFileCommentImage,
    removeFileCommentImage,
    prepareMessage,
    consumePreparedAttachments,
  } = annotations
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
  // Per-chat truncated text of the last assistant response, from the same
  // `chat_result_ready` snippet already used for the toast body. Session-only
  // (not persisted, not backfilled from `/api/chats`): a chat that finished
  // before this tab loaded shows no preview until its next turn completes.
  // Each entry is stamped with the chat's activity time as of the event, so
  // chatLastSnippet can tell a live snippet from one that has since been
  // superseded by a fresher server record (a missed event while the WS was
  // down must not keep an old preview pinned over the newer persisted one).
  const lastResultSnippet = ref<Record<string, string>>({})
  const lastResultSnippetAt = ref<Record<string, string>>({})
  // Chats whose snippet was stamped with client receipt time (the event
  // arrived before the chat existed in the local list, so its server
  // activity time was unknown). Receipt time has millisecond precision and
  // the backend truncates activity to whole seconds, so a delayed delivery
  // would otherwise keep the stale cached snippet authoritative forever —
  // the first reconcile of the chat rebases the stamp to its real
  // last_activity_at.
  const lastResultSnippetNeedsRebase = new Set<string>()
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
  // Per-chat count of live `background_run_start` command runs. Driven by
  // `chat_background_runs` over /ws/events and re-seeded from the snapshot.
  // Separate from `backgroundAgents`: a background run has no transcript and
  // nothing to open, so it gets a count-only indicator. Without it the chat
  // goes fully idle the moment the turn ends, with nothing saying a command
  // is still going — the tool is non-blocking by design.
  const backgroundRuns = ref<Record<string, number>>({})
  // Full-screen restart overlay while the server drains active chats and
  // relaunches. Driven by /ws/events `server_restarting` (and the same
  // signal on the per-chat socket when a send is rejected mid-drain).
  const serverRestarting = ref(false)
  const serverRestartMessage = ref('')
  // Ephemeral client-mode connection state. Host proxy failures must never
  // enter chat history: reconnect attempts can repeat indefinitely and would
  // otherwise create one error bubble (and one "Fix this error" action) each.
  const hostConnectionUnavailable = ref(false)
  const hostAuthRequired = ref(false)
  const hostPolicyBlocked = ref(false)
  // How many ChatPanels are on screen. ChatPanel renders its own
  // host-connection-card from the flag above, so the global banner uses this
  // to avoid announcing the same outage twice. A count, not a boolean: the
  // layout declares ChatPanel twice (mobile and desktop branches) and a
  // chat switch mounts the new panel before the old one unmounts.
  const chatPanelsMounted = ref(0)
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
  // `permission_response` on the per-chat WS and waits for the provider ack.
  const pendingPermissions = ref<Record<string, PendingPermission[]>>({})
  type PermissionResponse = {
    requestId: string
    sessionId: string
    approved: boolean
    reason: string
  }
  type PermissionSubmission = PermissionResponse & {
    pending: boolean
    queued: boolean
    error: string
    retryable: boolean
  }
  const permissionSubmissions = ref<Record<string, PermissionSubmission>>({})
  // Responses can be acknowledged after their socket disappears. Keep their
  // complete frames in memory only, keyed by chat + request, and flush them
  // when that chat reconnects. Never persist answers, reasons, or verdicts.
  const queuedPermissionResponses = new Map<string, PermissionResponse>()
  // Per-project "new chat is being created" flag so UI can disable buttons
  // and prevent double-clicks while the POST is in flight.
  const creatingChatProjectIds = ref<Record<string, boolean>>({})
  // Optimistic archiving: chats whose archive POST is in flight. They are
  // removed from active lists immediately and shown in the home "archiving…"
  // queue so the chat panel can close without waiting for the server's disk
  // work (the transcript write). The map is keyed by chat_id
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
  // AskUserQuestion arrives from headless providers with empty answers, so the
  // PWA renders its own picker above the composer. Native V2 forms remain
  // active until an acknowledged reply/cancel; legacy cards without a request
  // id are cleared by the next ordinary message.
  const activeQuestions = ref<Record<string, ActiveQuestion[]>>({})
  type QuestionResponse = {
    requestId: string
    sessionId: string
    action: 'reply' | 'cancel'
    answers: Record<string, string[]>
  }
  type QuestionSubmission = QuestionResponse & {
    pending: boolean
    queued: boolean
    error: string
    retryable: boolean
  }
  const questionSubmissions = ref<Record<string, QuestionSubmission>>({})
  // A native form blocks the turn until the server acknowledges its response.
  // Keep the complete frame outside reactive card state so a reconnect can
  // flush it without persisting potentially sensitive answers to localStorage.
  const queuedQuestionResponses = new Map<string, QuestionResponse>()
  const questionSubmissionTimers: Record<string, ReturnType<typeof setTimeout>> = {}
  const permissionSubmissionTimers: Record<string, ReturnType<typeof setTimeout>> = {}
  const RESPONSE_ACK_TIMEOUT_MS = 15_000

  function responseKey(
    chatId: string,
    requestId: string,
    sessionId = '',
  ) {
    return `${chatId}\u0000${sessionId}\u0000${requestId}`
  }

  function clearResponseTimer(
    timers: Record<string, ReturnType<typeof setTimeout>>,
    chatId: string,
    requestId: string,
    sessionId = '',
  ) {
    const key = responseKey(chatId, requestId, sessionId)
    const timer = timers[key]
    if (timer !== undefined) {
      clearTimeout(timer)
      delete timers[key]
    }
  }

  function armResponseTimer(
    timers: Record<string, ReturnType<typeof setTimeout>>,
    chatId: string,
    requestId: string,
    sessionId: string,
    onTimeout: () => void,
  ) {
    clearResponseTimer(timers, chatId, requestId, sessionId)
    const key = responseKey(chatId, requestId, sessionId)
    timers[key] = setTimeout(() => {
      delete timers[key]
      onTimeout()
    }, RESPONSE_ACK_TIMEOUT_MS)
  }

  function clearQuestionSubmissionTimer(
    chatId: string,
    requestId: string,
    sessionId = '',
  ) {
    clearResponseTimer(
      questionSubmissionTimers,
      chatId,
      requestId,
      sessionId,
    )
  }

  function armQuestionSubmissionTimer(
    chatId: string,
    requestId: string,
    sessionId: string,
  ) {
    armResponseTimer(
      questionSubmissionTimers,
      chatId,
      requestId,
      sessionId,
      () => {
        const current = questionSubmissions.value[chatId]
        if (
          !current
          || current.requestId !== requestId
          || (sessionId && current.sessionId !== sessionId)
          || !current.pending
        ) return
        queuedQuestionResponses.set(
          responseKey(chatId, requestId, current.sessionId),
          {
            requestId: current.requestId,
            sessionId: current.sessionId,
            action: current.action,
            answers: current.answers,
          },
        )
        questionSubmissions.value[chatId] = {
          ...current,
          pending: false,
          // Keep the in-memory frame queued. A reconnect after this timeout must
          // still deliver it; timeout only makes the card explicitly retryable.
          queued: true,
          error: 'No acknowledgement arrived; try again.',
        }
      },
    )
  }

  // Signatures of AskUserQuestion pickers the user has already answered or
  // dismissed this session, keyed by chat. Native cards remain visible until
  // the server acknowledges reply/cancel; the signature prevents a reconnect
  // snapshot from resurrecting a card after that acknowledgement. Legacy
  // provider cards without a request id are still cleared optimistically on
  // the next ordinary message.
  const resolvedQuestions = ref<Record<string, Set<string>>>({})

  // Record the currently-active picker for `chatId` as resolved. Reads the live
  // `activeQuestions` entry, so it must run before that entry is deleted.
  function markResolvedQuestion(
    chatId: string,
    requestId = '',
    sessionId = '',
  ) {
    const questions = activeQuestions.value[chatId]
    const scoped = requestId && questions
      ? questions.filter(question => (
        question.requestId === requestId
        && (!sessionId || !question.sessionId || question.sessionId === sessionId)
      ))
      : questions
    const sig = questionsSignature(scoped)
    if (!sig) return
    ;(resolvedQuestions.value[chatId] ||= new Set<string>()).add(sig)
  }

  function clearPermission(
    chatId: string,
    requestId: string,
    sessionId = '',
  ) {
    const submission = permissionSubmissions.value[chatId]
    const list = pendingPermissions.value[chatId]
    const matching = list?.find(permission => (
      permission.request_id === requestId
      && (!sessionId || !permission.session_id || permission.session_id === sessionId)
    ))
    const conflicting = list?.some(permission => (
      permission.request_id === requestId
      && Boolean(sessionId)
      && Boolean(permission.session_id)
      && permission.session_id !== sessionId
    ))
    if (
      conflicting
      || (submission && (
        submission.requestId !== requestId
        || (sessionId && submission.sessionId && submission.sessionId !== sessionId)
      ))
    ) return

    if (list) {
      const next = list.filter(permission => !(
        permission.request_id === requestId
        && (!sessionId || !permission.session_id || permission.session_id === sessionId)
      ))
      if (next.length) pendingPermissions.value[chatId] = next
      else delete pendingPermissions.value[chatId]
    }
    if (!submission || submission.requestId === requestId) {
      delete permissionSubmissions.value[chatId]
    }
    const effectiveSession = sessionId
      || matching?.session_id
      || submission?.sessionId
      || ''
    clearResponseTimer(
      permissionSubmissionTimers,
      chatId,
      requestId,
      effectiveSession,
    )
    queuedPermissionResponses.delete(
      responseKey(chatId, requestId, effectiveSession),
    )
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (chat?.pending_permission) {
      try {
        const stored = JSON.parse(chat.pending_permission) as {
          request_id?: string
          session_id?: string
        }
        if (
          (!stored.request_id || stored.request_id === requestId)
          && (
            !effectiveSession
            || !stored.session_id
            || stored.session_id === effectiveSession
          )
        ) chat.pending_permission = ''
      } catch {
        // A malformed persisted card has no request identity to match. Keep
        // it for a fresh server snapshot instead of claiming it was resolved.
      }
    }
  }

  function clearQuestion(
    chatId: string,
    requestId = '',
    sessionId = '',
  ) {
    const questions = activeQuestions.value[chatId]
    const submission = questionSubmissions.value[chatId]
    const matching = questions?.find(question => (
      question.requestId === requestId
      && (!sessionId || !question.sessionId || question.sessionId === sessionId)
    ))
    const conflicting = questions?.some(question => (
      question.requestId === requestId
      && Boolean(sessionId)
      && Boolean(question.sessionId)
      && question.sessionId !== sessionId
    ))
    if (
      conflicting
      || (requestId && submission && (
        submission.requestId !== requestId
        || (sessionId && submission.sessionId && submission.sessionId !== sessionId)
      ))
    ) return

    markResolvedQuestion(chatId, requestId, sessionId)
    delete activeQuestions.value[chatId]
    if (!requestId || !submission || submission.requestId === requestId) {
      delete questionSubmissions.value[chatId]
    }
    const effectiveSession = sessionId
      || matching?.sessionId
      || submission?.sessionId
      || ''
    if (requestId) {
      clearQuestionSubmissionTimer(chatId, requestId, effectiveSession)
      queuedQuestionResponses.delete(
        responseKey(chatId, requestId, effectiveSession),
      )
    } else {
      for (const question of questions || []) {
        clearQuestionSubmissionTimer(
          chatId,
          question.requestId,
          question.sessionId || '',
        )
        queuedQuestionResponses.delete(
          responseKey(chatId, question.requestId, question.sessionId || ''),
        )
      }
    }
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (chat?.pending_question) {
      try {
        const stored = JSON.parse(chat.pending_question) as {
          request_id?: string
          session_id?: string
        }
        if (
          (!requestId || !stored.request_id || stored.request_id === requestId)
          && (
            !effectiveSession
            || !stored.session_id
            || stored.session_id === effectiveSession
          )
        ) {
          chat.pending_question = ''
        }
      } catch {
        if (!requestId) chat.pending_question = ''
      }
    }
  }

  // ── Image-capability questions ────────────────────────────────────────
  // Shape and parsing live in `lib/chatQuestions.ts`; the live card is here.
  const activeCapabilityQuestions = ref<Record<string, CapabilityQuestion[]>>({})

  // Restore the AskUserQuestion picker after a reload. The picker lives in
  // ephemeral `activeQuestions` (set only by the live stream), but the server
  // persists the unanswered question on the chat, so we rebuild from there on
  // chat open. Never clobbers a picker already populated by the live stream.
  function rebuildPendingQuestion(chatId: string) {
    const chat = chats.value.find(c => c.chat_id === chatId)
    const qs = parseQuestions(
      chat?.pending_question,
      '',
      chat?.session_id || '',
    )
    if (!qs.length) {
      // The persisted chat is authoritative. A disconnected tab can miss the
      // ephemeral resolution frame; do not leave a settled blocking card up.
      // A partial client-side test/boot object may not carry the field yet;
      // wait for a real chat snapshot before treating that as settlement.
      if (
        chat
        && Object.prototype.hasOwnProperty.call(chat, 'pending_question')
        && activeQuestions.value[chatId]?.length
      ) clearQuestion(chatId)
      return
    }
    // Don't resurrect a picker the user already answered/dismissed from a
    // server snapshot that hasn't caught up yet.
    if (resolvedQuestions.value[chatId]?.has(questionsSignature(qs))) return
    const current = activeQuestions.value[chatId]
    if (current && questionsSignature(current) === questionsSignature(qs)) return
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
    if (!raw) {
      if (
        chat
        && Object.prototype.hasOwnProperty.call(chat, 'pending_permission')
        && pendingPermissions.value[chatId]?.length
      ) {
        delete pendingPermissions.value[chatId]
      }
      return
    }
    let parsed: { request_id?: string; session_id?: string; tool_name?: string; message?: string; tool_input?: string }
    try {
      parsed = JSON.parse(raw)
    } catch {
      return
    }
    if (!parsed.request_id) return
    const list = pendingPermissions.value[chatId] || []
    if (list.some(p => p.request_id === parsed.request_id)) return
    pendingPermissions.value[chatId] = [
      ...list.filter(p => p.request_id !== parsed.request_id),
      {
        request_id: parsed.request_id,
        session_id: parsed.session_id || undefined,
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
        gws_profile: '',
      }))
    }
    return [
      { name: 'personal', vault_root: 'personal', default_provider: 'claude', gws_profile: 'personal' },
      { name: 'work', vault_root: 'work', default_provider: 'claude', gws_profile: 'work' },
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

  const activeBackgroundRuns = computed(() =>
    backgroundRuns.value[activeChatId.value || ''] || 0
  )

  // Paused while the read-only subagent view is open for this chat: that
  // view already polls its single transcript on a 4s timer, so the store's
  // full-chat poll (every subagent, every transcript, re-parsed) would run
  // alongside it and defeat the narrowing the dedicated endpoint provides.
  const subagentViewActiveChatId = ref<string | null>(null)
  function setSubagentViewActive(chatId: string | null) {
    subagentViewActiveChatId.value = chatId
  }

  // Live view while subagents run: refresh the active chat's subagent
  // transcripts on a short interval so the panel updates as the agents
  // work. The CLI appends to the transcript files continuously, so polling
  // the REST endpoint is enough for a near-live feed. Runs while the active
  // chat has running background agents OR is streaming a turn (agents
  // dispatched mid-turn nest live inside the Working trace).
  let subagentPollTimer: ReturnType<typeof setInterval> | null = null
  watch(
    () => [
      activeChatId.value,
      activeBackgroundAgents.value,
      isStreaming.value,
      subagentViewActiveChatId.value,
    ] as const,
    ([chatId, count, streamingNow, viewChatId]) => {
      if (subagentPollTimer !== null) {
        clearInterval(subagentPollTimer)
        subagentPollTimer = null
      }
      if (!chatId || (count <= 0 && !streamingNow)) return
      if (viewChatId !== null && viewChatId === chatId) return
      subagentPollTimer = setInterval(() => {
        void loadSubagents(chatId)
      }, 4000)
    },
  )

  // Both polls below are owned by a watcher, which means nothing clears them
  // when the store itself goes away — the watcher stops, but an interval it
  // already created keeps firing. In the app the store outlives everything so
  // it never mattered; under test it does, because each test replaces the
  // pinia instance without disposing the old one and the orphaned intervals
  // then fire on the real clock for the rest of the run. That is what made the
  // bounded-drain test flaky: it counts poll requests either side of a fake
  // timer advance, and leaked intervals from earlier tests added real-clock
  // requests in between whenever the machine was loaded enough for the advance
  // to take real milliseconds.
  // Anything that outlives the watcher or handler that created it registers its
  // undo here. The app has exactly one immortal store so none of this ever
  // mattered in production, but under test each case leaks into every later
  // test in the file: pinia replaces the active instance without disposing the
  // old one, so a store built by test 3 keeps its real-clock timers and its
  // window listeners for the rest of the run.
  const teardowns: Array<() => void> = []

  /** Register an interval and its clear in one call. */
  function everyMs(fn: () => void, ms: number): void {
    const id = window.setInterval(fn, ms)
    teardowns.push(() => window.clearInterval(id))
  }

  /** Register a listener and its removal in one call. */
  function listen(
    target: EventTarget,
    type: string,
    handler: EventListenerOrEventListenerObject,
  ): void {
    target.addEventListener(type, handler)
    teardowns.push(() => target.removeEventListener(type, handler))
  }

  onScopeDispose(() => {
    if (subagentPollTimer !== null) {
      clearInterval(subagentPollTimer)
      subagentPollTimer = null
    }
    stopRunningSubagentPoll()
    for (const timer of Object.values(questionSubmissionTimers)) clearTimeout(timer)
    for (const timer of Object.values(permissionSubmissionTimers)) clearTimeout(timer)
    for (const undo of teardowns.splice(0)) {
      try { undo() } catch { /* a disposed store must not throw */ }
    }
  })

  // Anything working anywhere: a streaming turn or background agents. Gates
  // the running-subagent poll so an idle app makes no requests at all.
  const anyChatWorking = computed(() =>
    Object.values(streaming.value).some(Boolean)
    || Object.values(projectStreaming.value).some(Boolean)
    || Object.values(backgroundAgents.value).some(n => n > 0),
  )

  // The UI-facing "is anything happening?" — deliberately NOT `anyChatWorking`,
  // which is the poll gate above. A background run is the assistant working and
  // must light the nav item, but it is not a subagent, so folding it into the
  // gate would poll /api/subagents/running for rows that cannot exist.
  const anyChatBusy = computed(() =>
    anyChatWorking.value
    || Object.values(backgroundRuns.value).some(n => n > 0),
  )

  // The server keeps a row "running" until the agent's transcript goes idle,
  // which lags the parent turn by up to FINISHED_AGENT_IDLE_SECONDS (60s). So
  // one refresh on the falling edge was not enough: whatever it still listed
  // stayed in the sidebar with a live spinner until the next turn. Drain
  // instead — keep polling until the rows are gone — but only for a bounded
  // window, because a row the server can never retire (an interrupted agent
  // whose transcript has no final record) would otherwise poll forever. The
  // bound counts interval ticks rather than reading the wall clock: under
  // vitest's fake timers the two can disagree, and a deadline taken from the
  // real clock silently never expires (CI run 33378792898).
  const RUNNING_SUBAGENT_DRAIN_MS = 90_000
  const RUNNING_SUBAGENT_POLL_MS = 4000
  const RUNNING_SUBAGENT_DRAIN_TICKS = Math.ceil(RUNNING_SUBAGENT_DRAIN_MS / RUNNING_SUBAGENT_POLL_MS)

  let runningSubagentTimer: ReturnType<typeof setInterval> | null = null
  let runningSubagentRefreshGeneration = 0
  function stopRunningSubagentPoll(): void {
    if (runningSubagentTimer !== null) {
      clearInterval(runningSubagentTimer)
      runningSubagentTimer = null
    }
  }
  watch(anyChatWorking, (working) => {
    stopRunningSubagentPoll()
    // Refresh on both edges: rising paints the rows without waiting a tick,
    // falling starts the drain that clears the last of them.
    void refreshRunningSubagents()
    if (working) {
      runningSubagentTimer = setInterval(() => {
        void refreshRunningSubagents()
      }, RUNNING_SUBAGENT_POLL_MS)
      return
    }
    let drainTicksLeft = RUNNING_SUBAGENT_DRAIN_TICKS
    runningSubagentTimer = setInterval(() => {
      if (Object.keys(runningSubagents.value).length === 0 || drainTicksLeft <= 0) {
        stopRunningSubagentPoll()
        return
      }
      drainTicksLeft -= 1
      void refreshRunningSubagents()
    }, RUNNING_SUBAGENT_POLL_MS)
  })

  function projectChats(projectId: string): ChatInfo[] {
    // Hide remote chats (session lives on another device, not openable here).
    return chats.value
      .filter(c => c.project_id === projectId && !c.archived && c.local !== false)
      .sort((a, b) => a.created_at.localeCompare(b.created_at))
  }

  function chatActivity(chat: ChatInfo): string {
    return chat.last_activity_at || chat.created_at
  }

  // Most recent (max 5) non-archived chats in the active workspace.
  const recentChats = computed<ChatInfo[]>(() => {
    const wsProjectIds = new Set(workspaceProjects.value.map(p => p.project_id))
    return chats.value
      .filter(c => !c.archived && c.local !== false && wsProjectIds.has(c.project_id))
      .filter(c => Boolean(chatActivity(c)))
      .sort((a, b) => chatActivity(b).localeCompare(chatActivity(a)))
      .slice(0, 5)
  })

  // Full "jump back in" list for the home screen: every non-archived local
  // chat with activity, across ALL workspaces, newest first (uncapped). The
  // home surface is a global hub, so unlike recentChats it isn't scoped to
  // the active workspace — each chat carries its own workspace/project tag.
  const activeChatsAll = computed<ChatInfo[]>(() => {
    return chats.value
      .filter(c => !c.archived && c.local !== false)
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

  function chatHasBackgroundRuns(chatId: string): boolean {
    return (backgroundRuns.value[chatId] || 0) > 0
  }

  // Subagents this chat has working right now, from /api/subagents/running.
  // Sidebar rows and the per-row "working" signal read this; it is deliberately
  // separate from `subagents` (full transcripts, active chat only) because the
  // sidebar covers every chat and must stay cheap to keep fresh.
  function runningSubagentsFor(chatId: string): RunningSubagent[] {
    return runningSubagents.value[chatId] || []
  }

  function chatHasRunningSubagents(chatId: string): boolean {
    return runningSubagentsFor(chatId).length > 0
  }

  /**
   * Is this chat doing anything right now?
   *
   * One definition for every "working" surface. Each consumer used to OR the
   * sources by hand, so adding one meant finding them all — and the last
   * addition (background runs) reached the home lanes but not the project
   * header, which is why the two disagreed for a chat whose only activity was
   * a running subagent. Add new activity sources here, not at the call sites.
   */
  function chatIsWorking(chatId: string): boolean {
    return isChatStreaming(chatId)
      || chatHasBackgroundAgents(chatId)
      || chatHasBackgroundRuns(chatId)
      || chatHasRunningSubagents(chatId)
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

  /** Archived chats whose post-archive pipeline still has unfinished stages. */
  function insightsFailedChats(): ChatInfo[] {
    return chatsMatching(c => postprocessNeedsRetry(c.postprocess))
  }

  /** Insights-failed count for one workspace, for the home lane header. */
  function workspaceInsightsFailedCount(ws: WorkspaceName): number {
    return workspaceCountMatching(ws, c => postprocessNeedsRetry(c.postprocess))
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
        // The server is not running this pipeline. Use the manifest to tell a
        // clean settle from an interrupted one: an unfinished job stays
        // retryable rather than being reported as done.
        const state = pp.job?.unfinished?.length
          ? (pp.job.state === 'blocked' ? 'blocked' : 'incomplete')
          : 'done'
        chat.postprocess = { ...pp, state, step: '' }
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
            linkUrl: status.source || undefined,
            linkLabel: 'What\u2019s new',
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

  // Cmd+T picker: open a fresh, empty chat in the chosen project, switching to
  // its workspace first if needed. Returns the created chat, or undefined when
  // the project could not be found.
  async function newChatInProject(
    projectId: string,
    initialText = '',
    title = DEFAULT_CHAT_TITLE,
  ): Promise<ChatInfo | undefined> {
    const project = projects.value.find(p => p.project_id === projectId)
    if (!project) {
      pushErrorToast('Cannot open a new chat', 'No project found to create the chat in.')
      return
    }
    const previousWorkspace = activeWorkspace.value
    const previousChatId = activeChatId.value
    const crossing = project.workspace !== previousWorkspace
    if (crossing && previousChatId) {
      disconnectWs(previousChatId)
    }
    activeWorkspace.value = project.workspace
    persistState()
    try {
      return initialText
        ? await createChat(project.project_id, title, initialText)
        : await createChat(project.project_id)
    } catch (err) {
      // The switch is committed before the POST, so a rejected creation used
      // to leave the app scoped to the new workspace while still showing (and
      // having just disconnected) a chat from the old one. Put it back.
      if (crossing) {
        activeWorkspace.value = previousWorkspace
        persistState()
        if (previousChatId && activeChatId.value === previousChatId) {
          connectWs(previousChatId)
        }
      }
      throw err
    }
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

  function reconcileQueuedWithMessages(chatId: string) {
    const list = queuedMessages.value[chatId]
    if (!list?.length) return
    const chatMessages = messages.value[chatId] || []
    const remaining = list.filter(q => !queuedTextAlreadyRendered(chatMessages, q.text))
    if (remaining.length) queuedMessages.value[chatId] = remaining
    else delete queuedMessages.value[chatId]
  }

  // ── Persistence ─────────────────────────────────────────────────────

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
      // Comments, pins and staged images: same reads, same order, still
      // inside this try — a malformed value aborts the rest of the restore
      // exactly as it did when the reads were inline.
      annotations.restoreFromStorage()
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

  function persistState() {
    try {
      localStorage.setItem('ciao-active-workspace', activeWorkspace.value)
      if (activeChatId.value) localStorage.setItem('ciao-active-chat', activeChatId.value)
      else localStorage.removeItem('ciao-active-chat')
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
  function chatNeedsInput(chatId: string): boolean {
    if (activeQuestions.value[chatId]?.length) return true
    if (pendingPermissions.value[chatId]?.length) return true
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (parseQuestions(chat?.pending_question).length > 0) return true
    return Boolean(chat?.pending_permission)
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

  // Preview text for an unread chat's home tile. Prefers the session-only WS
  // value (see `lastResultSnippet` above) so a snippet that lands while this
  // tab is open always wins; falls back to the persisted `last_snippet` from
  // `/api/chats` so a chat that finished before this tab loaded still shows
  // a preview. The persisted record wins when it is NEWER than the cached
  // event snippet: this tab can miss a `chat_result_ready` (WS gap) and then
  // reconcile a chat record whose `last_snippet` is the newer response — the
  // stale session-only preview must not keep overriding it.
  function chatLastSnippet(chatId: string): string | null {
    const chat = chats.value.find(c => c.chat_id === chatId)
    const cached = lastResultSnippet.value[chatId]
    if (!chat?.last_snippet) return cached || null
    if (!cached) return chat.last_snippet
    const cachedAt = lastResultSnippetAt.value[chatId] || ''
    const persistedAt = chat.last_activity_at || ''
    // Persisted wins on ties too: the backend truncates activity timestamps
    // to whole seconds, so two responses finishing in the same second leave
    // persistedAt === cachedAt — and a tie with the refreshed record means
    // the cached snippet is the OLDER response this tab caught live while
    // missing the newer event. The same-turn persisted snippet is equivalent
    // to the live one, so equality can only cost the stale case.
    return persistedAt && cachedAt && persistedAt >= cachedAt
      ? chat.last_snippet
      : cached
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

  // Deliberate "come back to this": clears the server-side read stamp so the
  // chat re-enters the unread state on every device, and locally so the dot
  // appears without waiting for the WS echo. Opening the chat marks it read
  // again through the normal path.
  async function markUnread(chatId: string) {
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat) return
    chat.last_read_at = ''
    try {
      await api.post(`/api/chats/${chatId}/unread`, {})
    } catch { /* fire-and-forget; next fetchAll will reconcile */ }
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
      void refreshRunningSubagents()
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
        // Boot only. fetchAll is also a refresh — SchedulePanel calls it
        // while the app is running — and clearing the selection there dropped
        // the user's open chat just because the current route was /schedules,
        // sending them to the home screen when they navigated back.
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
        everyMs(() => { void checkPackageStatus() }, UPDATE_CHECK_INTERVAL_MS)
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

    // Rebase any receipt-time snippet stamps now that the server's real
    // activity times are available (see lastResultSnippetNeedsRebase).
    if (lastResultSnippetNeedsRebase.size) {
      for (const chat of nextChats) {
        if (lastResultSnippetNeedsRebase.has(chat.chat_id) && chat.last_activity_at) {
          lastResultSnippetAt.value[chat.chat_id] = chat.last_activity_at
          lastResultSnippetNeedsRebase.delete(chat.chat_id)
        }
      }
    }

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
    // Do not gate on visibility — answers must appear in the active chat
    // even while the tab is hidden, otherwise the reply stays invisible
    // until the user opens the Activity view (visibilitychange).
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
    listen(navigator.serviceWorker, 'controllerchange', () => {
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

  // Archive, never delete: the server unregisters the workspace, archives its
  // chats and moves its folder intact into `.archived-workspaces/`. Its
  // projects leave through `project_deleted` events; they and their chats are
  // also dropped here so a missed frame cannot leave them in the sidebar.
  async function archiveWorkspace(name: WorkspaceName) {
    // Captured before the request: the `project_deleted` frames can land
    // while it is in flight and remove the projects and chats this needs.
    const projectIds = new Set(
      projects.value.filter(p => p.workspace === name).map(p => p.project_id),
    )
    const selectedChatId = activeChatId.value
    const selected = activeChat.value
    const selectedInWorkspace = !!selected && projectIds.has(selected.project_id)
    const res = await api.post<WorkspacesResponse & { archived?: { path: string } }>(
      `/api/workspaces/${encodeURIComponent(name)}/archive`,
    )
    workspaces.value = res.workspaces || []
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
    if (activeWorkspace.value === name) {
      activeWorkspace.value = res.active || workspaces.value[0]?.name || 'personal'
    }
    projects.value.forEach(p => { if (p.workspace === name) projectIds.add(p.project_id) })
    projects.value = projects.value.filter(p => !projectIds.has(p.project_id))
    projectIds.forEach(clearDraftsForProject)
    chats.value = chats.value.filter(c => !projectIds.has(c.project_id))
    if (selectedInWorkspace && selectedChatId) {
      disconnectWs(selectedChatId)
      if (activeChatId.value === selectedChatId) activeChatId.value = null
      persistState()
      // Leave Settings where it is; only a view of the archived chat itself
      // has to move somewhere valid.
      const { router } = await import('../router')
      if (router.currentRoute.value.params.chatId === selectedChatId) {
        await transitionToFirstChat()
      }
    }
    return res
  }

  async function fetchArchivedWorkspaces(): Promise<ArchivedWorkspace[]> {
    const res = await api.get<ArchivedWorkspacesResponse>('/api/workspaces/archived')
    return res.archived || []
  }

  async function restoreArchivedWorkspace(id: string) {
    const res = await api.post<WorkspacesResponse & {
      restored?: { schedules_paused?: number; schedules_dropped?: number }
    }>('/api/workspaces/archived/restore', { id })
    workspaces.value = res.workspaces || []
    workspaceProviderOptions.value = res.provider_options?.length
      ? res.provider_options
      : [{ value: 'claude', label: 'Claude' }]
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
    if (annotations.hasStagedAttachments(chatId)) return false
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
      let deleted = false
      try {
        deleted = await deleteChat(chatId, { selectNext: false, onlyIfEmpty: true })
      } finally {
        // The view is already cleared. A failed DELETE must not also strand
        // the router on /chat/<id> with no active chat behind it. But the
        // user may have moved on while the DELETE was in flight:
        //   - a different chat is now active: leave it alone entirely.
        //   - this chat was reopened and the delete succeeded: it is gone
        //     server-side, so the dangling activeChatId must be cleared -
        //     regardless of where the user has since navigated.
        //   - this chat was reopened and the delete was declined (no longer
        //     empty): it is a real chat again, leave it alone entirely.
        const shouldClear = wasActive && (activeChatId.value === null
          || (deleted && activeChatId.value === chatId))
        if (shouldClear) {
          activeChatId.value = null
          persistState()
          // Only force the `/` navigation from a route this close owns: the
          // closed chat's own route, or a bare chat route where the push is a
          // no-op. Settings/Schedules/Memory/Proposals/a project retain
          // activeChatId across navigation by design, and pages like /device
          // sit outside ChatLayout entirely - forcing a `/` push onto any of
          // them would eject the user from wherever they've gone, even though
          // the stale id above still needed clearing. `/chat/<other>` is the
          // same hazard: selecting a chat navigates first and only sets
          // activeChatId once the async open finishes, so a late cleanup can
          // see a null id with the router already on the new chat.
          const { router } = await import('../router')
          const path = router.currentRoute.value.path
          if (path === '/' || path === '/chat' || path === `/chat/${chatId}`) {
            await router.push('/')
          }
        }
      }
      return
    }
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

  async function archiveChat(chatId: string) {
    // Guard against double-clicks: the button is already disabled while the
    // optimistic state is live, but event handlers can still double-fire.
    if (archivingChats.value[chatId]) return
    // Remember whether we actually closed the socket. If the POST fails the
    // chat is still live, and a closed socket is marked as an intentional
    // close so nothing auto-reconnects it — the chat would go silent, with no
    // tokens, permission cards or AskUserQuestion prompts, and no sign
    // anything broke.
    const hadSocket = Boolean(sockets.value[chatId])
    // Snapshot for rollback if the POST fails. A failure must not leave the
    // chat hidden from the sidebar with archived:true and no transcript.
    const chatBefore = chats.value.find(ch => ch.chat_id === chatId)
    const prevArchived = chatBefore ? chatBefore.archived : false
    const wasActive = activeChatId.value === chatId
    // Optimistic UI: hide from active lists immediately and show in the
    // home "archiving…" queue so the panel can close without waiting for
    // the server's disk work (the transcript write).
    archivingChats.value[chatId] = true
    pendingArchived.value.add(chatId)
    if (chatBefore) chatBefore.archived = true
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

    let res: ArchiveChatResponse
    try {
      res = await api.post<ArchiveChatResponse>(`/api/chats/${chatId}/archive`)
    } catch (e) {
      // Roll back optimistic mutation: chat reappears in the sidebar / home
      // active lanes and its socket is put back so streaming resumes.
      const c = chats.value.find(ch => ch.chat_id === chatId)
      if (c) c.archived = prevArchived
      delete archivingChats.value[chatId]
      pendingArchived.value.delete(chatId)
      if (hadSocket) connectWs(chatId)
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

    if (res?.postprocess) {
      const c = chats.value.find(ch => ch.chat_id === chatId)
      // The response closes the race where the chat_postprocess event is
      // emitted after the archive request has already cleared this pane.
      if (c) c.postprocess = res.postprocess
    }
    delete archivingChats.value[chatId]
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

  /** Resume the unfinished post-archive steps for one archived chat. */
  async function retryInsights(chatId: string): Promise<void> {
    const res = await api.post<{ status: string; job?: ArchiveJobView | null }>(
      `/api/chats/${chatId}/retry-insights`,
    )
    const status = res?.status
    if (res?.job) applyArchiveJob(chatId, res.job)
    if (status === 'running') {
      pushToast({ chat_id: '', title: 'Already tidying', body: 'This chat is already being processed.' })
    } else if (status === 'complete') {
      pushToast({ chat_id: '', title: 'Nothing to finish', body: 'Every post-archive step is already complete.' })
    } else if (status === 'blocked') {
      pushToast({
        chat_id: '',
        title: 'Cannot resume yet',
        body: res?.job?.blocked_reason || 'This chat needs attention before its unfinished steps can run.',
      })
    }
  }

  /** Fold a manifest view onto the chat's postprocess record. */
  function applyArchiveJob(chatId: string, job: ArchiveJobView | null | undefined) {
    if (!job) return
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat) return
    const pp: ChatPostprocess = { ...(chat.postprocess || { state: 'done' }) }
    pp.job = job
    if (job.unfinished?.length) {
      if (pp.state !== 'running') {
        pp.state = job.state === 'blocked' ? 'blocked' : 'incomplete'
      }
    } else if (pp.state === 'incomplete' || pp.state === 'blocked') {
      // The server confirmed nothing is unfinished (e.g. a completion event was
      // missed). Clear a stale incomplete/blocked state, or the UI keeps
      // showing "not finished" and a retry control forever.
      pp.state = 'done'
      pp.step = ''
    }
    chat.postprocess = pp
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
          // A local cache holding only un-indexed rows (the optimistic user
          // bubble on a brand-new chat's first turn, say) has no firstIndex
          // to merge by - but only adopt the window wholesale once the
          // server actually has rows to offer. An empty window here just
          // means the turn hasn't persisted yet (still "Thinking..."), and
          // wholesale-adopting it would wipe the pending optimistic bubble
          // until the next reload repopulates it.
          (typeof firstIndex !== 'number' && windowRows.length > 0) ||
          (typeof firstIndex === 'number' && env.total <= firstIndex) ||
          // The server assembled FEWER rows than we hold - a pruned or
          // unreadable session segment. Merging by index would refresh the
          // prefix and leave the stale tail untouched, showing messages that
          // are no longer part of the chat, so the window wins outright.
          (typeof firstIndex === 'number' && env.total < cachedEnd)
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
          // Where the window's genuinely-new rows start once appended; -1 when
          // the window brought nothing past the cached extent.
          let firstAppendPos = -1
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
          const followUpBoundary = (() => {
            const users = merged
              .map((row, pos) => row.role === 'user' && pos >= tailStart ? pos : -1)
              .filter(pos => pos >= 0)
            if (users.length > 1) return users[1]
            if (users.length !== 1) return null
            const pos = users[0]
            if (typeof merged[pos].i === 'number') return pos
            return merged.slice(tailStart, pos).some(isLiveTraceRow) ? pos : null
          })()
          let insertedBeforeFollowUp = 0
          for (const item of windowRows) {
            const abs = item.i
            if (typeof abs !== 'number') continue
            const pos = posByIndex.get(abs)
            if (pos !== undefined) {
              // The server row is authoritative for content, but its footer
              // facts land only once `record_turn` has written the turn to the
              // durable transcript. Keep whatever the live stream already gave
              // us for the fields the row is still missing.
              merged[pos] = mergeMessageFields(item, merged[pos])
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
            // Insert settled rows before a fast follow-up's live tail. Without
            // this, the follow-up renders before the authoritative answer it
            // followed, even though both turns are otherwise reconciled.
            const insertionPos = followUpBoundary === null
              ? merged.length
              : followUpBoundary + insertedBeforeFollowUp
            if (firstAppendPos < 0) firstAppendPos = insertionPos
            for (const [index, position] of posByIndex) {
              if (position >= insertionPos) posByIndex.set(index, position + 1)
            }
            merged.splice(insertionPos, 0, item)
            posByIndex.set(abs, insertionPos)
            if (followUpBoundary !== null) insertedBeforeFollowUp++
          }
          messages.value[chatId] = dropSupersededLiveTail(merged, tailStart, firstAppendPos)
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
    // Retries exhausted with the transcript still ending in a user row or bare
    // tool activity. That is exactly what a stopped turn looks like when it
    // produced no reply, and the loop used to just give up -- leaving the
    // spinner running over a turn the server had already finished, until the
    // next send silently replaced it. The server says this chat is idle, so
    // the turn is over whatever the last row is.
    if (!projectStreaming.value[chatId]) clearStreamingState(chatId)
    void loadSubagents(chatId)
  }

  // ── Subagents ───────────────────────────────────────────────────────

  // Both subagent loaders write `subagents.value[chatId]`, on independent 4s
  // timers, and one replaces the whole array while the other merges a single
  // row into it. Without ordering, a response that left earlier can land later
  // and undo the newer one — visibly, as the open transcript losing its newest
  // messages.
  //
  // One monotonic sequence orders every request across both loaders, but each
  // guards only against *its own* out-of-order responses: a shared
  // last-writer-wins counter would have let the fast single-agent poll
  // invalidate a slow full-set fetch outright, so a chat with a large session
  // file never refreshed its other agents while the subagent view was open.
  // Cross-loader ordering is handled per row instead: the full set keeps any
  // row a narrower, newer response already wrote.
  let subagentSeq = 0
  const subagentListSeq = new Map<string, number>()
  const subagentRowSeq = new Map<string, number>()
  const subagentRowApplied = new Map<string, number>()
  const rowKey = (chatId: string, agentId: string) =>
    `${chatId}|${bareAgentId(agentId)}`

  async function loadSubagents(chatId: string): Promise<void> {
    const seq = ++subagentSeq
    subagentListSeq.set(chatId, seq)
    try {
      const r = await api.get<SubagentTranscript[]>(`/api/chats/${chatId}/subagents`)
      if (subagentListSeq.get(chatId) !== seq) return
      const fresh = Array.isArray(r) ? r : []
      const prior = subagents.value[chatId] || []
      subagents.value[chatId] = fresh.map((row) => {
        // A single-agent response that landed after this request was issued is
        // newer for that row, whatever order the two responses arrived in.
        if ((subagentRowApplied.get(rowKey(chatId, row.agent_id)) || 0) <= seq) return row
        return prior.find(s => sameAgent(s.agent_id, row.agent_id)) || row
      })
    } catch {
      // No session locally / SDK error — leave any prior data in place.
    }
  }

  // One agent's transcript, for the read-only subagent view. That view polls
  // while its agent works, and the unfiltered endpoint renders every subagent
  // the chat ever spawned — so on a chat with a dozen finished agents the
  // whole set was re-fetched every few seconds to redraw one of them. Merges
  // into the existing list rather than replacing it, so the in-chat panel's
  // data for the other agents survives.
  async function loadSubagent(chatId: string, agentId: string): Promise<void> {
    if (!agentId) return
    const key = rowKey(chatId, agentId)
    const seq = ++subagentSeq
    subagentRowSeq.set(key, seq)
    try {
      const r = await api.get<SubagentTranscript[]>(
        `/api/chats/${chatId}/subagents?agent_id=${encodeURIComponent(agentId)}`,
      )
      if (subagentRowSeq.get(key) !== seq) return
      const fetched = Array.isArray(r) ? r : []
      const one = fetched.find(s => sameAgent(s.agent_id, agentId))
      if (!one) return
      subagentRowApplied.set(key, seq)
      const prior = subagents.value[chatId] || []
      const idx = prior.findIndex(s => sameAgent(s.agent_id, agentId))
      subagents.value[chatId] = idx === -1
        ? [...prior, one]
        : prior.map((s, i) => (i === idx ? one : s))
    } catch {
      // No session locally / SDK error — leave any prior data in place.
    }
  }

  // Live subagents across every working chat, for the sidebar rows. Replaced
  // wholesale rather than merged: the server omits chats with nothing running,
  // and that omission is exactly how a finished subagent's row disappears.
  async function refreshRunningSubagents(): Promise<void> {
    const generation = ++runningSubagentRefreshGeneration
    try {
      const r = await api.get<RunningSubagentsResponse>('/api/subagents/running')
      if (generation !== runningSubagentRefreshGeneration) return
      runningSubagents.value = (r && typeof r === 'object' && r.chats) ? r.chats : {}
    } catch {
      // Transient (offline, host proxy down): keep the last known rows rather
      // than blanking the sidebar on one failed poll.
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
    // Every caller of this function is reacting to a route the browser or the
    // router has already applied, so a sub-view named in that route is the
    // destination — not something to navigate out of. See switchChat.
    await switchChat(chatId, { keepSubView: true })
  }

  /**
   * `skipHistory` is for a chat this client just created: its history is
   * provably empty, so the /messages round-trip can only return nothing while
   * the "Loading conversation" skeleton sits on screen for its duration. Every
   * other entry point still fetches.
   */
  async function switchChat(
    chatId: string,
    opts?: { skipHistory?: boolean; keepSubView?: boolean },
  ) {
    await ensureWorkspaceForChat(chatId)
    // Always sync URL, even if activeChatId already matches (we may have
    // landed here from /settings or /schedules where the chat route isn't
    // currently active).
    const { router } = await import('../router')
    const currentRoute = router.currentRoute.value
    const currentRouteChatId = currentRoute.params.chatId
    // `/chat/:chatId/subagent/:agentId` carries the same `chatId`, so matching
    // on that alone made clicking the chat's own sidebar row a no-op while one
    // of its subagents was open: no push, and the activeChatId guard below
    // returns early. Any sub-view of the chat still has to navigate back to it.
    //
    //
    // `keepSubView` is the exception, and it is what makes the subagent view
    // reachable for a chat that is not already active. Opening
    // `/chat/B/subagent/Y` navigates first and only then reaches here (through
    // the route watcher / deep-link path), so the `agentId` this would strip is
    // the one the user just asked for — the view would render for a frame and
    // bounce to the chat, and a cold reload of that URL would never render it
    // at all. A direct sidebar click on the chat's own row passes no flag and
    // still leaves the sub-view, which is what that click means.
    const keepSubView = Boolean(opts?.keepSubView) && currentRouteChatId === chatId
    if ((currentRouteChatId !== chatId || currentRoute.params.agentId) && !keepSubView) {
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
      hostAuthRequired.value = false
      hostPolicyBlocked.value = false
      lastChatFrameAt[chatId] = nowMs()
      sendFocus(chatId)
      flushQueuedResponses(chatId)
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
        hostAuthRequired.value = false
        hostPolicyBlocked.value = false
        return
      }
      if (event.type === 'error' && isHostPolicyMessage(event.message)) {
        hostAuthRequired.value = false
        hostPolicyBlocked.value = true
        hostConnectionUnavailable.value = false
        return
      }
      if (event.type === 'auth_required') {
        hostAuthRequired.value = true
        hostPolicyBlocked.value = false
        hostConnectionUnavailable.value = false
        return
      }
      if (
        event.type !== 'host_unreachable'
        && !(event.type === 'error' && isHostConnectionUnavailableMessage(event.message))
      ) {
        hostConnectionUnavailable.value = false
        hostAuthRequired.value = false
        hostPolicyBlocked.value = false
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

    ws.onclose = (event: CloseEvent) => {
      const isCurrent = toRaw(sockets.value[chatId]) === ws
      if (isCurrent) {
        delete sockets.value[chatId]
        delete lastChatFrameAt[chatId]
        // The answer may have reached the server just before the socket died.
        // Keep the complete frame so onopen can retry it instead of silently
        // losing a response that blocks the native V2 form.
        const submission = questionSubmissions.value[chatId]
        if (submission?.pending && !submission.queued) {
          queueQuestionResponse(chatId, submission)
        }
        const permission = permissionSubmissions.value[chatId]
        if (permission?.pending && !permission.queued) {
          queuePermissionResponse(chatId, permission)
        }
      }

      const wasIntentional = intentionalCloses.delete(ws)
      if (wasIntentional) return
      if (!isCurrent) return
      if (isWsAuthClose(event?.code)) {
        hostAuthRequired.value = true
        hostPolicyBlocked.value = false
        hostConnectionUnavailable.value = false
        return
      }
      if (isWsPolicyClose(event?.code)) {
        hostPolicyBlocked.value = true
        hostAuthRequired.value = false
        hostConnectionUnavailable.value = false
        return
      }

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
    everyMs(checkWsLiveness, WS_LIVENESS_CHECK_MS)

    // WKWebView can keep the document visible while the native window loses
    // key focus (another app, hide, or minimize). Report those standard
    // browser focus transitions so the engine does not suppress a native
    // notification for a chat the user cannot currently see.
    listen(window, 'focus', () => {
      if (activeChatId.value) sendFocus(activeChatId.value)
    })
    listen(window, 'blur', () => {
      if (activeChatId.value) sendFocus(activeChatId.value)
    })

    listen(document, 'visibilitychange', () => {
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
    listen(window, 'pageshow', ((ev: Event) => {
      if ((ev as PageTransitionEvent).persisted || document.visibilityState === 'visible') {
        void resumeActiveChat()
        checkPendingTarget()
      }
    }) as EventListener)

    if (typeof navigator !== 'undefined' && 'serviceWorker' in navigator) {
      listen(navigator.serviceWorker, 'message', ((ev: Event) => {
        const data = (ev as MessageEvent).data
        if (data && data.type === 'open-chat' && data.chat_id) {
          void openChatFromDeepLink(data.chat_id)
        } else if (data && data.type === 'pending-target' && data.chat_id) {
          void openChatFromDeepLink(data.chat_id)
        }
      }) as EventListener)
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
  // Separate from the handshake streak above: a client-mode proxy accepts
  // the browser socket and only then discovers the host is down, so those
  // retries must not feed the >=5 auth probe. Reset by the first real frame.
  let eventsHostRetryAttempts = 0

  function connectEventsWs() {
    if (eventsSocket.value && eventsSocket.value.readyState <= WebSocket.OPEN) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/events`)
    eventsSocket.value = ws
    lastEventsFrameAt = nowMs()
    let opened = false
    // Set when the local proxy told us the host is down on THIS socket. The
    // proxy accepts the browser's connection before it tries the host, so
    // `opened` is true even for a dead host -- without this flag the close
    // handler below would take the 50ms "healthy blip" path and reconnect
    // twenty times a second for as long as the host stays away.
    let hostUnreachable = false

    ws.onopen = () => {
      if (toRaw(eventsSocket.value) !== ws) return
      opened = true
      hostAuthRequired.value = false
      hostPolicyBlocked.value = false
      eventsWsFailureStreak = 0
      lastEventsFrameAt = nowMs()
    }

    ws.onmessage = (ev) => {
      if (toRaw(eventsSocket.value) !== ws) return
      // Any frame (including the server keepalive) proves the socket is live.
      lastEventsFrameAt = nowMs()
      let msg: EventsWsMessage
      try { msg = JSON.parse(ev.data) } catch { return }
      if (msg.type === 'host_unreachable') {
        // In client mode this is the only connection-loss signal that exists
        // outside a chat: the per-chat socket is open only while a chat is on
        // screen, so the home screen used to look perfectly healthy while the
        // host was unreachable.
        hostUnreachable = true
        hostConnectionUnavailable.value = true
        hostAuthRequired.value = false
        hostPolicyBlocked.value = false
        return
      }
      if (msg.type === 'auth_required') {
        hostAuthRequired.value = true
        hostPolicyBlocked.value = false
        hostConnectionUnavailable.value = false
        return
      }
      // Any other frame -- the keepalive included -- travelled through the
      // proxy from the host, which proves the host is back.
      hostConnectionUnavailable.value = false
      hostAuthRequired.value = false
      hostPolicyBlocked.value = false
      eventsHostRetryAttempts = 0
      if (msg.type === 'keepalive') return
      handleEventsMessage(msg)
    }

    ws.onclose = (event: CloseEvent) => {
      const isCurrent = toRaw(eventsSocket.value) === ws
      if (isCurrent) {
        eventsSocket.value = null
      }
      if (!isCurrent) return
      if (isWsAuthClose(event?.code)) {
        hostAuthRequired.value = true
        hostPolicyBlocked.value = false
        hostConnectionUnavailable.value = false
        return
      }
      if (isWsPolicyClose(event?.code)) {
        hostPolicyBlocked.value = true
        hostAuthRequired.value = false
        hostConnectionUnavailable.value = false
        return
      }

      if (opened) {
        if (hostUnreachable) {
          // Retry on the chat socket's backoff curve (50ms -> 2s cap) so a
          // host that comes back is noticed within a couple of seconds
          // without hammering it while it is down.
          eventsHostRetryAttempts += 1
          const hostDelay = chatWsReconnectDelayMs(eventsHostRetryAttempts)
          setTimeout(() => {
            if (!eventsSocket.value) connectEventsWs()
          }, hostDelay)
          return
        }
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

  // A single automation lifecycle call can fan out several `schedules_changed`
  // frames (create-then-enable), and each one would otherwise cost a full
  // GET /api/schedules per open tab. Coalesce a burst into one refetch. The
  // tasks store is imported lazily to keep it out of this module's import graph.
  let schedulesRefetchTimer: ReturnType<typeof setTimeout> | null = null
  function scheduleSchedulesRefetch(): void {
    if (schedulesRefetchTimer !== null) return
    schedulesRefetchTimer = setTimeout(() => {
      schedulesRefetchTimer = null
      void import('./tasks')
        .then(({ useTaskStore }) => useTaskStore().fetchSchedules())
        .catch(() => {})
    }, 150)
  }

  // Bumped when another tab or device archived or restored a workspace, so
  // views holding their own copy of the registry (Settings) refetch it too.
  const workspaceRegistryRevision = ref(0)
  let workspacesRefetchTimer: ReturnType<typeof setTimeout> | null = null
  function scheduleWorkspacesRefetch(): void {
    if (workspacesRefetchTimer !== null) return
    workspacesRefetchTimer = setTimeout(() => {
      workspacesRefetchTimer = null
      void refreshWorkspaceRegistry().catch(() => {})
    }, 150)
  }

  // The workspace list and the projects in it, from the server. An archived
  // workspace's projects are gone from /api/projects; a restored one's
  // General project appears there.
  async function refreshWorkspaceRegistry(): Promise<void> {
    const [, fresh] = await Promise.all([
      fetchWorkspaces(),
      api.get<ProjectInfo[]>('/api/projects'),
    ])
    const known = new Set(fresh.map(p => p.project_id))
    const dropped = projects.value.filter(p => !known.has(p.project_id)).map(p => p.project_id)
    projects.value = fresh
    if (dropped.length) {
      const gone = new Set(dropped)
      dropped.forEach(clearDraftsForProject)
      const selected = activeChat.value
      chats.value = chats.value.filter(c => !gone.has(c.project_id))
      if (selected && gone.has(selected.project_id) && activeChatId.value === selected.chat_id) {
        disconnectWs(selected.chat_id)
        activeChatId.value = null
        persistState()
      }
    }
    workspaceRegistryRevision.value++
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
        // Same, for tracked command runs. Authoritative for the same reason,
        // and it matters more here: a run outlives the turn that started it,
        // so the snapshot is how a client learns about one that was already
        // going when it connected. (A server restart does not carry runs
        // over — the runner resolves them as orphans — so an empty map after
        // one is the truth, not a gap.)
        backgroundRuns.value = { ...(msg.background_runs || {}) }
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
          if (msg.snippet) {
            lastResultSnippet.value[msg.chat_id] = msg.snippet
            if (resultChat) {
              // The chat record has not reconciled yet, so its current
              // last_activity_at IS this turn's activity time. Once the
              // reconciled record is newer, the persisted snippet wins.
              lastResultSnippetAt.value[msg.chat_id] = resultChat.last_activity_at || ''
            } else {
              // Chat unknown locally (created on another client): stamp with
              // receipt time and flag for rebase on the chat's first
              // reconcile — receipt time is millisecond-precise while the
              // server truncates to whole seconds, so without the rebase a
              // delayed event would pin this snippet over a fresher record.
              lastResultSnippetAt.value[msg.chat_id] = new Date().toISOString()
              lastResultSnippetNeedsRebase.add(msg.chat_id)
            }
          }
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
        // Ensure the active chat's transcript refreshes even when the tab is
        // hidden — otherwise the answer stays invisible until the user opens
        // the Activity view (which fires a visibilitychange).
        if (msg.chat_id === activeChatId.value) {
          void reconcileAfterResult(msg.chat_id)
        }
        break
      }
      case 'chat_background_runs': {
        // Count-only: there is no transcript to pull and no agent row to
        // refresh, unlike `chat_subagents_ready`. The finishing run delivers
        // its own wake turn, which arrives as a normal chat result.
        if (msg.running > 0) {
          backgroundRuns.value[msg.chat_id] = msg.running
        } else {
          delete backgroundRuns.value[msg.chat_id]
        }
        break
      }
      case 'chat_subagents_ready': {
        // The sidebar's subagent rows come from a poll; this is the same
        // signal a tick later, so use it to redraw immediately.
        void refreshRunningSubagents()
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
      case 'chat_unread': {
        // Another tab/device marked this chat unread on purpose ("come back
        // to this"): raise the dot and badge here too. No overlay write — the
        // server field is the state and the getter derives unread from it.
        const chat = chats.value.find(c => c.chat_id === msg.chat_id)
        if (chat) chat.last_read_at = msg.last_read_at
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
        // Read before the chats are filtered: afterwards `activeChat` no
        // longer finds the chat and the selection would never be cleared.
        const selectedChatId = activeChat.value?.project_id === msg.project_id
          ? activeChatId.value
          : null
        projects.value = projects.value.filter(p => p.project_id !== msg.project_id)
        chats.value = chats.value.filter(c => c.project_id !== msg.project_id)
        if (selectedChatId) {
          disconnectWs(selectedChatId)
          activeChatId.value = null
          persistState()
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
      case 'workspaces_changed': {
        // A workspace was archived or restored in another tab or device.
        // Refetch the registry so the sidebar and pickers stop offering it
        // (or show it again) without a reload.
        scheduleWorkspacesRefetch()
        break
      }
      case 'schedules_changed': {
        // An automation was created/edited/paused/resumed/deleted elsewhere
        // (the model mid-turn, the Automations page, another tab). Refetch so
        // the chat's automation banner and the sidebar/home markers appear
        // without a manual reload.
        scheduleSchedulesRefetch()
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
    // A native V2 form owns the turn until its reply/cancel is acknowledged.
    // Do not silently dismiss it by sending an unrelated composer message.
    if (activeQuestions.value[chatId]?.some(q => q.requestId)) {
      pushToast({
        chat_id: chatId,
        title: 'Answer the open question first',
        body: 'The model is waiting for the highlighted question before it can continue.',
        variant: 'error',
      })
      return false
    }
    // Claude's legacy picker has no request id and is intentionally dismissed
    // by the next ordinary message.
    if (activeQuestions.value[chatId]) {
      clearQuestion(chatId)
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

  function armPermissionSubmissionTimer(
    chatId: string,
    requestId: string,
    sessionId: string,
  ) {
    armResponseTimer(
      permissionSubmissionTimers,
      chatId,
      requestId,
      sessionId,
      () => {
        const current = permissionSubmissions.value[chatId]
        if (
          !current
          || current.requestId !== requestId
          || (sessionId && current.sessionId !== sessionId)
          || !current.pending
        ) return
        queuedPermissionResponses.set(
          responseKey(chatId, requestId, current.sessionId),
          {
            requestId: current.requestId,
            sessionId: current.sessionId,
            approved: current.approved,
            reason: current.reason,
          },
        )
        permissionSubmissions.value[chatId] = {
          ...current,
          pending: false,
          queued: true,
          error: 'No acknowledgement arrived; try again.',
        }
      },
    )
  }

  function queuePermissionResponse(chatId: string, response: PermissionResponse) {
    queuedPermissionResponses.set(
      responseKey(chatId, response.requestId, response.sessionId),
      response,
    )
    const current = permissionSubmissions.value[chatId]
    if (
      !current
      || (
        current.requestId === response.requestId
        && (!response.sessionId
          || !current.sessionId
          || current.sessionId === response.sessionId)
      )
    ) {
      permissionSubmissions.value[chatId] = {
        ...response,
        pending: true,
        queued: true,
        error: '',
        retryable: true,
      }
    }
    armPermissionSubmissionTimer(
      chatId,
      response.requestId,
      response.sessionId,
    )
  }

  function sendPermissionResponse(chatId: string, response: PermissionResponse) {
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      queuePermissionResponse(chatId, response)
      return false
    }
    try {
      const payload: Record<string, unknown> = {
        type: 'permission_response',
        request_id: response.requestId,
        approved: response.approved,
        reason: response.reason,
      }
      if (response.sessionId) payload.session_id = response.sessionId
      ws.send(JSON.stringify(payload))
      queuedPermissionResponses.delete(
        responseKey(chatId, response.requestId, response.sessionId),
      )
      const current = permissionSubmissions.value[chatId]
      if (
        !current
        || (
          current.requestId === response.requestId
          && (!response.sessionId
            || !current.sessionId
            || current.sessionId === response.sessionId)
        )
      ) {
        permissionSubmissions.value[chatId] = {
          ...response,
          pending: true,
          queued: false,
          error: '',
          retryable: true,
        }
      }
      armPermissionSubmissionTimer(
        chatId,
        response.requestId,
        response.sessionId,
      )
      return true
    } catch {
      queuePermissionResponse(chatId, response)
      return false
    }
  }

  function respondPermission(
    chatId: string,
    requestId: string,
    approved: boolean,
    reason = '',
    sessionId = '',
  ) {
    const request = pendingPermissions.value[chatId]?.find(permission => (
      permission.request_id === requestId
      && (!sessionId || !permission.session_id || permission.session_id === sessionId)
    ))
    if (!request) return false
    const current = permissionSubmissions.value[chatId]
    if (
      current?.requestId === requestId
      && (!sessionId || !current.sessionId || current.sessionId === sessionId)
      && current.pending
    ) return true
    return sendPermissionResponse(chatId, {
      requestId,
      sessionId: request.session_id || '',
      approved,
      reason,
    })
  }

  function queueQuestionResponse(chatId: string, response: QuestionResponse) {
    queuedQuestionResponses.set(
      responseKey(chatId, response.requestId, response.sessionId),
      response,
    )
    const current = questionSubmissions.value[chatId]
    if (
      !current
      || (
        current.requestId === response.requestId
        && (!response.sessionId
          || !current.sessionId
          || current.sessionId === response.sessionId)
      )
    ) {
      questionSubmissions.value[chatId] = {
        ...response,
        pending: true,
        queued: true,
        error: '',
        retryable: true,
      }
    }
    armQuestionSubmissionTimer(
      chatId,
      response.requestId,
      response.sessionId,
    )
  }

  function sendQuestionResponse(chatId: string, response: QuestionResponse) {
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      queueQuestionResponse(chatId, response)
      return false
    }
    try {
      const payload: Record<string, unknown> = {
        type: 'question_response',
        request_id: response.requestId,
        action: response.action,
        answers: response.action === 'cancel' ? {} : response.answers,
      }
      if (response.sessionId) payload.session_id = response.sessionId
      ws.send(JSON.stringify(payload))
      queuedQuestionResponses.delete(
        responseKey(chatId, response.requestId, response.sessionId),
      )
      const current = questionSubmissions.value[chatId]
      if (
        !current
        || (
          current.requestId === response.requestId
          && (!response.sessionId
            || !current.sessionId
            || current.sessionId === response.sessionId)
        )
      ) {
        questionSubmissions.value[chatId] = {
          ...response,
          answers: response.action === 'cancel' ? {} : response.answers,
          pending: true,
          queued: false,
          error: '',
          retryable: true,
        }
      }
      armQuestionSubmissionTimer(
        chatId,
        response.requestId,
        response.sessionId,
      )
      return true
    } catch {
      queueQuestionResponse(chatId, response)
      return false
    }
  }

  function flushQueuedResponses(chatId: string) {
    const prefix = `${chatId}\u0000`
    const questions = [...queuedQuestionResponses.entries()]
      .filter(([key]) => key.startsWith(prefix))
    for (const [key, response] of questions) {
      queuedQuestionResponses.delete(key)
      sendQuestionResponse(chatId, response)
    }
    const permissions = [...queuedPermissionResponses.entries()]
      .filter(([key]) => key.startsWith(prefix))
    for (const [key, response] of permissions) {
      queuedPermissionResponses.delete(key)
      sendPermissionResponse(chatId, response)
    }
  }

  function respondQuestion(
    chatId: string,
    requestId: string,
    answers: Record<string, string[]>,
    action: 'reply' | 'cancel' = 'reply',
    sessionId = '',
  ) {
    const question = activeQuestions.value[chatId]?.find(candidate => (
      candidate.requestId === requestId
      && (!sessionId || !candidate.sessionId || candidate.sessionId === sessionId)
    ))
    if (!question) return false
    const current = questionSubmissions.value[chatId]
    if (
      current?.requestId === requestId
      && (!sessionId || !current.sessionId || current.sessionId === sessionId)
      && current.pending
    ) return true
    return sendQuestionResponse(chatId, {
      requestId,
      sessionId: question.sessionId || '',
      action,
      answers,
    })
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
      if (annotations.isAutoPinDismissed(chatId, raw)) return
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
          const qs = parseQuestions(
            event.tool_input,
            event.request_id || '',
            event.session_id || '',
          )
          if (qs.length) {
            const signature = questionsSignature(qs)
            const currentQuestions = activeQuestions.value[chatId]
            const sameRequest = Boolean(
              event.request_id
              && currentQuestions?.some(
                question => question.requestId === event.request_id
                && (!event.session_id || !question.sessionId || question.sessionId === event.session_id),
              ),
            )
            const sameLegacyQuestion = !event.request_id
              && questionsSignature(currentQuestions) === signature
            // Broker replay is common after reconnect. Replacing the array for
            // the same request makes the component watcher erase in-progress
            // answers and can cancel the retry state while its frame is queued.
            if (
              sameRequest
              || sameLegacyQuestion
              || resolvedQuestions.value[chatId]?.has(signature)
            ) break

            delete resolvedQuestions.value[chatId]
            const submission = questionSubmissions.value[chatId]
            if (!submission?.pending) delete questionSubmissions.value[chatId]
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
          ? `${toolIcon(event.tool_name)} ${event.tool_name} ${event.tool_input}`
          : `${toolIcon(event.tool_name)} ${event.tool_name}`

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

      case 'question_response_result': {
        const submission = questionSubmissions.value[chatId]
        const matches = submission?.requestId === event.request_id
          && (!event.session_id || !submission.sessionId || submission.sessionId === event.session_id)
        if (!matches || !submission) break
        const responseSession = event.session_id || submission.sessionId
        queuedQuestionResponses.delete(
          responseKey(chatId, event.request_id, responseSession),
        )
        clearQuestionSubmissionTimer(
          chatId,
          event.request_id,
          responseSession,
        )
        if (event.ok) {
          clearQuestion(chatId, event.request_id, responseSession)
        } else {
          questionSubmissions.value[chatId] = {
            ...submission,
            pending: false,
            queued: false,
            error: event.error || 'OpenCode rejected the answer; retry the form.',
            retryable: event.retryable !== false,
          }
        }
        break
      }

      case 'question_resolved': {
        const question = activeQuestions.value[chatId]?.find(
          candidate => candidate.requestId === event.request_id
            && (!event.session_id || !candidate.sessionId || candidate.sessionId === event.session_id),
        )
        if (question) {
          clearQuestion(chatId, event.request_id, question.sessionId || '')
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
          // A turn the user stopped is not an answer. Its partial text still
          // renders so the transcript matches what they watched arrive, but
          // badging it would put an unread marker on the half sentence they
          // just cancelled -- on their other devices too, since every client
          // receives this frame.
          if (!isActive && !event.stopped) {
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
        // A terminal SSE frame is not proof that an OpenCode permission was
        // delivered. Keep the card until its response result or authoritative
        // resolution event arrives.
        persistMessages()
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

      case 'permission_response_result': {
        const submission = permissionSubmissions.value[chatId]
        const matches = submission?.requestId === event.request_id
          && (!event.session_id || !submission.sessionId || submission.sessionId === event.session_id)
        if (!matches || !submission) break
        const responseSession = event.session_id || submission.sessionId
        queuedPermissionResponses.delete(
          responseKey(chatId, event.request_id, responseSession),
        )
        clearResponseTimer(
          permissionSubmissionTimers,
          chatId,
          event.request_id,
          responseSession,
        )
        if (event.ok) {
          clearPermission(chatId, event.request_id, responseSession)
        } else {
          permissionSubmissions.value[chatId] = {
            ...submission,
            pending: false,
            queued: false,
            error: event.error || 'The permission reply failed; retry the request.',
            retryable: event.retryable !== false,
          }
        }
        break
      }

      case 'permission_resolved': {
        const request = pendingPermissions.value[chatId]?.find(
          permission => permission.request_id === event.request_id
            && (!event.session_id || !permission.session_id || permission.session_id === event.session_id),
        )
        if (request) {
          clearPermission(
            chatId,
            event.request_id,
            request.session_id || event.session_id || '',
          )
        }
        break
      }

      case 'permission_request': {
        const list = pendingPermissions.value[chatId] || []
        const existing = list.find(permission =>
          permission.request_id === event.request_id
          && (!event.session_id || !permission.session_id || permission.session_id === event.session_id),
        )
        if (existing) break
        // Auto mode classifier escalated: model wants to run a tool, pop the
        // Approve/Deny bubble. Keep a visible timeline line too so the user
        // sees the context even if they dismiss the buttons by scrolling.
        _commitStreamingTextToTimeline(chatId)
        _pushToolLine(chatId, `\u{1F6A7} Permission: ${event.tool_name} - ${event.message}`)
        pendingPermissions.value[chatId] = [
          ...list,
          {
            request_id: event.request_id,
            session_id: event.session_id,
            tool_name: event.tool_name,
            tool_input: event.tool_input || '',
            message: event.message,
            received_at: Date.now(),
          },
        ]
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

  restoreState()
  restoreUnread()

  return {
    // State
    projects, chats, workspaces, workspaceProviderOptions, activeWorkspace, activeChatId, bootstrapped, messages, messageHistoryLoading, subagents, unread, lastResultSnippet, lastResultSnippetAt, lastResultSnippetNeedsRebase,
    streaming, streamingText, streamingThinking, pendingImages, pendingComments, pendingChatComments, fileComments, queuedMessages,
    projectStreaming, backgroundAgents, backgroundRuns, runningSubagents, toasts, pendingPermissions, permissionSubmissions, activeQuestions, questionSubmissions, activeCapabilityQuestions, creatingChatProjectIds,
    serverRestarting, serverRestartMessage, hostConnectionUnavailable, hostAuthRequired, hostPolicyBlocked, chatPanelsMounted,
    // Computed
    workspaceProjects, workspaceOptions, activeChat, activeProject, activeMessages, activeSubagents,
    isStreaming, currentStreamingText, currentStreamingThinking, currentQueued, activeBackgroundAgents, activeBackgroundRuns, currentActivity, currentTimeline, currentLiveUsage, currentStreamStartedAt, projectChats,
    chatUnread, chatNeedsInput, chatPendingQuestion, chatLastSnippet, projectNeedsInput, projectUnread, workspaceUnread, workspaceNeedsInput, totalUnread, attentionChatCount, clearUnread, markRead, markUnread, markAllRead,
    recentChats, activeChatsAll, projectIsStreaming, isChatStreaming, chatHasBackgroundAgents, chatHasBackgroundRuns, runningSubagentsFor, chatHasRunningSubagents, chatIsWorking, anyChatBusy, workspaceIsStreaming, projectFor,
    chatPostprocess, chatIsPostprocessing, postprocessingChats, workspacePostprocessingCount, projectPostprocessingCount,
    insightsFailedChats, workspaceInsightsFailedCount,
    archivingChats, isArchiving, archivingChatsList, workspaceArchivingCount, projectArchivingCount,
    // Actions
    fetchAll, fetchWorkspaces, createWorkspace, updateWorkspace,
    archiveWorkspace, fetchArchivedWorkspaces, restoreArchivedWorkspace,
    workspaceRegistryRevision, refreshWorkspaceRegistry,
    createProject, updateProject, reorderProjects, deleteProject, completeProject,
    fetchCompletedProjects, restoreProject,
    generalProject,
    createChat, newChatInGeneral, newChatInProject, renameChat, updateChat, handoverChat, forkChat, moveChat, deleteChat, closeChat, archiveChat, continueArchivedChat, newSession,
    setChatRetry, stopChatRetry, tryChatRetryNow, retryInsights,
    switchChat, switchWorkspace, openChatFromDeepLink, ensureWorkspaceForChat,
    syncLatest, reconcileChatList,
    sendMessage, stopChat, respondPermission, respondQuestion, respondCapability, markResolvedQuestion, transcribeVoice, speakMessage, uploadImages, uploadImageRefs, addPendingImageRefs, removePendingImage, clearPendingImages,
    addPendingComment, removePendingComment, clearPendingComments,
    addPendingChatComment, removePendingChatComment, clearPendingChatComments, updatePendingChatComment,
    addPendingChatCommentImage, removePendingChatCommentImage,
    addFileCommentImage, removeFileCommentImage,
    fileCommentsFor, removeFileComment, updateFileComment,
    pinFile, unpinFile, pinnedFileFor,
    removeQueued, removeQueuedById, reorderQueued, editQueued, clearQueued,
    loadMessages, loadSubagents, loadSubagent, refreshRunningSubagents, setSubagentViewActive,
    canLoadOlder, isLoadingOlder, loadOlderMessages, expandMessagePart,
    connectWs, disconnectWs, connectEventsWs,
    beginServerRestart, restoreState,
    pushToast, pushErrorToast, dismissToast, fixError, restoreDraft,
    packageStatus, checkPackageStatus,
  }
})
