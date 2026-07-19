import { defineStore } from 'pinia'
import { ref, computed, watch, toRaw } from 'vue'
import { api } from '../lib/api'
import { getPendingBucket, normalizePendingBuckets, setPendingBucket } from '../lib/pendingBuckets'
import { buildFixPrompt } from '../lib/fixError'
import { formatChatComments, formatFileComments } from '../lib/commentContext'
import { isPlausibleFilePath } from '../lib/filePaths'
import {
  DEFAULT_RESTART_MESSAGE,
  isRestartDrainMessage,
  reloadWhenServerReady,
} from '../lib/serverRestart'
import type {
  ProjectInfo,
  ChatInfo,
  ChatMessage,
  SubagentTranscript,
  ProviderSubchatRecord,
  WsEvent,
  EventsWsMessage,
  VoiceResult,
  InAppToast,
  PendingPermission,
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

export const useProjectStore = defineStore('projects', () => {
  const projects = ref<ProjectInfo[]>([])
  const chats = ref<ChatInfo[]>([])
  const workspaces = ref<WorkspaceInfo[]>([])
  const workspaceProviderOptions = ref<WorkspaceProviderOption[]>([
    { value: 'claude', label: 'Claude' },
  ])
  // claude.ai connector MCP names the per-workspace toggle controls, for the
  // PWA Settings label. Populated from /api/workspaces; stable across writes.
  const workspaceClaudeAiConnectors = ref<string[]>([])
  // App-wide fallback model when a workspace default_model is empty; lets
  // Settings label the picker "Inherit default (<model>)".
  const workspaceAppDefaultModel = ref('')
  const activeWorkspace = ref<WorkspaceName>('personal')
  const activeChatId = ref<string | null>(null)
  // False until the first fetchAll() resolves. Gates the home empty state so
  // a restored active chat does not flash the getting-started screen.
  const bootstrapped = ref(false)
  const messages = ref<Record<string, ChatMessage[]>>({})
  // Subagent transcripts keyed by chat_id. Loaded lazily on chat switch and
  // after each streaming turn (subagents can be spawned mid-turn).
  const subagents = ref<Record<string, SubagentTranscript[]>>({})
  // Provider sub-chats keyed by parent chat_id.
  const providerSubchats = ref<Record<string, ProviderSubchatRecord[]>>({})
  // Provider sub-chat transcript events keyed by subchat_id.
  const providerSubchatEvents = ref<Record<string, any[]>>({})
  const sockets = ref<Record<string, WebSocket>>({})
  const streaming = ref<Record<string, boolean>>({})
  const streamingText = ref<Record<string, string>>({})
  const streamingTextPhase = ref<Record<string, ChatMessage['phase']>>({})
  // Per-chat in-flight thinking buffer. Mirrors `streamingText` but for
  // `thinking_delta` events: we accumulate the model's reasoning text and
  // commit it as a `kind: 'thinking'` timeline entry the moment a visible
  // text delta or tool_use starts (i.e. thinking has ended). Without this
  // buffer, intermediate thinking blocks emitted by Ollama models would
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
  type PendingChatComment = {
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
  // Pinned file paths per project_id. When a file is pinned it stays visible
  // in a side panel while chatting in that project.
  const pinnedFilePaths = ref<Record<string, string>>({})
  // 'filecard' carries a file-write tool call (Write/Edit/MultiEdit/NotebookEdit).
  // It breaks contiguous 'tool' groups so the PWA can render a standalone
  // clickable card with a preview link instead of folding it into _activity.
  type StreamEntry =
    | { kind: 'tool'; content: string }
    | { kind: 'thinking'; content: string }
    | { kind: 'text'; content: string; phase?: ChatMessage['phase'] }
    | { kind: 'filecard'; content: string; file_path: string; action: string; tool: string }
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
  // (at rebuild time). Native providers (Codex) carry a `requestId`; Claude's
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
  const eventsSocket = ref<WebSocket | null>(null)
  const toasts = ref<InAppToast[]>([])
  let toastCounter = 0

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
  let lastEventsFrameAt = 0
  const lastChatFrameAt: Record<string, number> = {}
  const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())
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
        model_bucket: '',
      }))
    }
    return [
      { name: 'personal', vault_root: 'personal', default_provider: 'claude', default_model: '', gws_profile: 'personal', model_bucket: 'personal' },
      { name: 'work', vault_root: 'work', default_provider: 'claude', default_model: '', gws_profile: 'work', model_bucket: 'work' },
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

  function isChatStreaming(chatId: string): boolean {
    return Boolean(projectStreaming.value[chatId] || streaming.value[chatId])
  }

  // Background subagents outlive the turn that spawned them; this powers the
  // sidebar/header indicators during the quiet gap where no turn is
  // streaming but agents are still working.
  function chatHasBackgroundAgents(chatId: string): boolean {
    return (backgroundAgents.value[chatId] || 0) > 0
  }

  function projectIsStreaming(projectId: string): boolean {
    return chats.value.some(c => c.project_id === projectId && isChatStreaming(c.chat_id))
  }

  function workspaceIsStreaming(ws: WorkspaceName): boolean {
    const wsProjectIds = new Set(projects.value.filter(p => p.workspace === ws).map(p => p.project_id))
    return chats.value.some(c => wsProjectIds.has(c.project_id) && isChatStreaming(c.chat_id))
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
  // the raw log seeded into a fix chat when the user clicks "Fix".
  function pushErrorToast(title: string, errorText: string): InAppToast {
    return pushToast({
      chat_id: '',
      title,
      body: errorText,
      variant: 'error',
      errorText,
    })
  }

  function dismissToast(id: number) {
    const idx = toasts.value.findIndex(t => t.id === id)
    if (idx >= 0) toasts.value.splice(idx, 1)
  }

  // Open a fresh chat in the active workspace's auto-managed General project,
  // pre-filled with a prompt asking the agent to diagnose and fix `errorText`
  // (falling back to a GitHub issue if the bug is in Ciaobot itself).
  async function fixError(opts: {
    errorText: string
    context?: string
    title?: string
  }): Promise<ChatInfo | undefined> {
    const general = projects.value.find(
      p => p.workspace === activeWorkspace.value && p.is_auto && p.name === 'General',
    )
    if (!general) {
      pushErrorToast(
        'Cannot open fix chat',
        'No General project found in this workspace to open a fix chat in.',
      )
      return
    }
    const chat = await createChat(general.project_id, opts.title || 'Fix error')
    const prompt = buildFixPrompt({ errorText: opts.errorText, context: opts.context })
    await sendMessage(chat.chat_id, prompt, 'queue')
    return chat
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

  function sanitizeCachedMessages() {
    let changed = false
    for (const [chatId, chatMessages] of Object.entries(messages.value)) {
      const nextMessages = normalizeMessages(chatMessages)
      if (JSON.stringify(nextMessages) !== JSON.stringify(chatMessages)) changed = true
      messages.value[chatId] = nextMessages
    }
    if (changed) persistMessages()
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
      const pi = localStorage.getItem('ciao-pending-images')
      if (pi) pendingImagesByChat.value = normalizePendingBuckets<string>(JSON.parse(pi), activeChatId.value)
      const pc = localStorage.getItem('ciao-pending-comments')
      if (pc) pendingCommentsByChat.value = normalizePendingBuckets<PendingComment>(JSON.parse(pc), activeChatId.value)
      const pcc = localStorage.getItem('ciao-pending-chat-comments')
      if (pcc) pendingChatCommentsByChat.value = normalizePendingBuckets<PendingChatComment>(JSON.parse(pcc), activeChatId.value)
      const ssa = localStorage.getItem('ciao-stream-started-at')
      if (ssa) streamStartedAt.value = JSON.parse(ssa)
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

  // Server-authoritative unread: a chat is unread if last_activity_at is
  // strictly newer than last_read_at. ISO-8601 timestamps compare correctly
  // as strings. The local `unread` ref is an optimistic overlay used for
  // offline push increments and between WS event and the server round-trip;
  // if set it wins. The getter returns 0 or 1 — the bell dropdown surfaces
  // the list, so an exact per-chat count isn't needed.
  function chatUnread(chatId: string): number {
    // Invariant: the chat the user is actively looking at is, by definition,
    // read. Suppress the badge regardless of the server's last_read_at. This
    // also closes a race in `chat_result_ready` where api.get('/api/chats')
    // can resolve before POST /read is processed and briefly roll back the
    // optimistic last_read_at update.
    if (chatId === activeChatId.value && documentVisible.value) return 0
    if (unread.value[chatId]) return 1
    const chat = chats.value.find(c => c.chat_id === chatId)
    if (!chat) return 0
    const activity = chat.last_activity_at || ''
    const read = chat.last_read_at || ''
    return activity && activity > read ? 1 : 0
  }

  // A chat blocked on AskUserQuestion — persisted on the chat and mirrored in
  // ephemeral activeQuestions while the picker is live. Unlike unread, this
  // stays visible even when the chat is the active tab.
  function chatNeedsInput(chatId: string): boolean {
    if (activeQuestions.value[chatId]?.length) return true
    const chat = chats.value.find(c => c.chat_id === chatId)
    return parseQuestions(chat?.pending_question).length > 0
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

  const totalUnread = computed(() =>
    chats.value.reduce((sum, c) => sum + (c.archived ? 0 : chatUnread(c.chat_id)), 0),
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
    try {
      navigator.serviceWorker?.controller?.postMessage({
        type: 'chat-focused',
        chat_id: chatId,
      })
    } catch { /* ignore */ }
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
    try {
      navigator.serviceWorker?.controller?.postMessage({ type: 'clear-badge' })
    } catch { /* ignore */ }
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
      workspaceClaudeAiConnectors.value = workspaceResponse.claude_ai_connectors || []
      projects.value = p
      reconcileChatList(c)
      const knownWorkspaceNames = workspaceOptions.value.map(w => w.name)
      if (!knownWorkspaceNames.includes(activeWorkspace.value)) {
        activeWorkspace.value = workspaceResponse.active || knownWorkspaceNames[0] || 'personal'
      }

      // Initial active-chat resolution priority:
      //   1) URL /chat/:chatId (represents the user's direct intent on a reload
      //      or deep link; must beat localStorage to avoid briefly subscribing
      //      to the wrong chat's WS and, on switch, rewriting the URL).
      //   2) activeChatId restored from localStorage (if it still exists and
      //      isn't archived).
      //   3) First chat in the current workspace.
      const { router } = await import('../router')
      const urlChatId = (router.currentRoute.value.params.chatId as string | undefined)
        || (typeof window !== 'undefined'
          ? window.location.pathname.match(/^\/chat\/([^/]+)/)?.[1]
          : undefined)
      if (urlChatId && chatExistsInList(urlChatId, c)) {
        await ensureWorkspaceForChat(urlChatId)
        activeChatId.value = urlChatId
      } else if (activeChatId.value && !chatExistsInList(activeChatId.value, c)) {
        activeChatId.value = null
      }
      if (!activeChatId.value) {
        selectFirstChat()
      }
      if (activeChatId.value) {
        void markRead(activeChatId.value)
        await loadMessages(activeChatId.value)
        connectWs(activeChatId.value)
      }
      // Open the cross-chat awareness socket once per app session.
      connectEventsWs()
      // If a push arrived while the PWA was closed/suspended and
      // notificationclick didn't fire (iOS quirk), the SW still has the
      // target chat cached. Query it and navigate if present.
      checkPendingTarget()
    } finally {
      bootstrapped.value = true
    }
  }

  function reconcileChatList(nextChats: ChatInfo[]) {
    chats.value = nextChats

    // Prune messages for deleted chats.
    const validIds = new Set(nextChats.map(ch => ch.chat_id))
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

  function hasSettledHistory(chatId: string): boolean {
    // Server still streaming this chat — session files already contain
    // mid-turn assistant progress text, which must not look "settled".
    if (projectStreaming.value[chatId]) return false
    const localMessages = messages.value[chatId] || []
    const last = localMessages[localMessages.length - 1]
    if (!last) return false
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
      const latestChats = await api.get<ChatInfo[]>('/api/chats')
      reconcileChatList(latestChats)

      const chatId = activeChatId.value
      const chatStillOpen = chatId
        ? latestChats.some(c => c.chat_id === chatId && !c.archived && c.local !== false)
        : false
      if (!chatId || !chatStillOpen) return

      await loadMessages(chatId)
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

  async function deleteProject(projectId: string) {
    const activeChatProject = activeChat.value?.project_id
    await api.del(`/api/projects/${projectId}`)
    projects.value = projects.value.filter(p => p.project_id !== projectId)
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

  async function createChat(projectId: string, title = 'New Chat') {
    creatingChatProjectIds.value[projectId] = true
    try {
      const c = await api.post<ChatInfo>(`/api/projects/${projectId}/chats`, { title })
      chats.value.push(c)
      messages.value[c.chat_id] = []
      switchChat(c.chat_id)
      return c
    } finally {
      delete creatingChatProjectIds.value[projectId]
    }
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
      provider?: 'claude' | 'codex'
      thinking_level?: string
      model_bucket?: string
    },
  ) {
    const c = await api.patch<ChatInfo>(`/api/chats/${chatId}`, updates)
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
  }

  async function handoverChat(
    chatId: string,
    updates: { model: string; provider: 'claude' | 'codex'; model_bucket?: string },
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

  async function deleteChat(chatId: string) {
    disconnectWs(chatId)
    await api.del(`/api/chats/${chatId}`)
    chats.value = chats.value.filter(c => c.chat_id !== chatId)
    delete messages.value[chatId]
    persistMessages()
    if (activeChatId.value === chatId) {
      await transitionToFirstChat()
    }
  }

  async function archiveChat(chatId: string) {
    disconnectWs(chatId)
    await api.post(`/api/chats/${chatId}/archive`)
    const idx = chats.value.findIndex(c => c.chat_id === chatId)
    if (idx >= 0) chats.value[idx].archived = true
    // Clear the active chat instead of auto-jumping to another one.
    // Auto-jumping caused a half-mounted state where the header showed
    // the newly-selected chat's title but the message list hadn't
    // loaded yet. Closing the chat (and letting ChatLayout open the
    // sidebar on mobile via the `close` event from ChatPanel) lets the
    // user pick the next chat themselves.
    if (activeChatId.value === chatId) {
      activeChatId.value = null
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

  function replaceChat(chat: ChatInfo) {
    const idx = chats.value.findIndex(x => x.chat_id === chat.chat_id)
    if (idx >= 0) chats.value[idx] = chat
    else chats.value.push(chat)
  }

  async function newSession(chatId: string) {
    const c = await api.post<ChatInfo>(`/api/chats/${chatId}/new`)
    const idx = chats.value.findIndex(x => x.chat_id === chatId)
    if (idx >= 0) chats.value[idx] = c
    messages.value[chatId] = []
    persistMessages()
    // Reconnect WebSocket for fresh session
    disconnectWs(chatId)
    connectWs(chatId)
  }

  // ── Message loading from server ──────────────────────────────────────

  async function loadMessages(chatId: string) {
    // Restore the AskUserQuestion picker before touching history. Runs on every
    // chat open / reconnect, so a reloaded chat paused on a question shows the
    // interactive picker again instead of the dead trace row. Independent of
    // server history, so it survives the early returns below.
    rebuildPendingQuestion(chatId)
    // Fetch authoritative history from the SDK session on the server.
    // This catches schedule outputs, turns from other devices, etc.
    try {
      const serverMsgs = await api.get<{ role: string; content: string; tool_name?: string; images?: string[]; turn_index?: number; sent_at?: string; duration_ms?: number; is_error?: boolean; file_path?: string; action?: string; tool?: string; phase?: 'commentary' | 'final_answer' }[]>(
        `/api/chats/${chatId}/messages`
      )
      if (!serverMsgs.length) {
        reconcileQueuedWithMessages(chatId)
        return
      }

      const normalizedServer = normalizeMessages(serverMsgs.map(m => ({
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
        // _filecard fields. Empty/undefined for non-file rows.
        file_path: m.file_path,
        action: m.action,
        tool: m.tool,
        phase: m.phase,
      })))
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

  // Post-result reconciliation: the SDK session file is sometimes a beat
  // behind the result event (buffered writes, WS reconnect races). Retry
  // loadMessages until the server's history ends with a final assistant
  // reply, so the bubble lands without needing a manual close/reopen.
  async function reconcileAfterResult(chatId: string) {
    const delays = [0, 300, 700, 1500, 3000, 5000]
    for (const delay of delays) {
      if (delay) await new Promise(r => setTimeout(r, delay))
      await loadMessages(chatId)
      const msgs = messages.value[chatId] || []
      const last = msgs[msgs.length - 1]
      // Stop once the turn is capped by a non-error assistant reply or an
      // explicit error/system note — anything that isn't a trailing user msg
      // or tool-activity entry means the final state is rendered.
      if (!last) continue
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
    void loadProviderSubchats(chatId)
  }

  async function loadProviderSubchats(chatId: string): Promise<void> {
    try {
      const r = await api.get<ProviderSubchatRecord[]>(`/api/chats/${chatId}/provider-subchats`)
      providerSubchats.value[chatId] = Array.isArray(r) ? r : []
    } catch {
      // ignore
    }
  }

  async function loadProviderSubchatEvents(subchatId: string): Promise<void> {
    try {
      const r = await api.get<any[]>(`/api/provider-subchats/${subchatId}/events`)
      providerSubchatEvents.value[subchatId] = Array.isArray(r) ? r : []
    } catch {
      // ignore
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
    await switchChat(chatId)
  }

  async function switchChat(chatId: string) {
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
    await loadMessages(chatId)
    void loadSubagents(chatId)
    connectWs(chatId)
  }

  async function switchWorkspace(ws: WorkspaceName, options?: { transition?: boolean }) {
    const transition = options?.transition !== false
    if (activeWorkspace.value === ws) return
    if (activeChatId.value) disconnectWs(activeChatId.value)
    activeWorkspace.value = ws
    if (transition) {
      persistState()
      await transitionToFirstChat()
    } else {
      selectFirstChat()
      persistState()
    }
  }

  // ── WebSocket ───────────────────────────────────────────────────────

  function _currentFocused(chatId: string): boolean {
    if (typeof document === 'undefined') return true
    return activeChatId.value === chatId && document.visibilityState === 'visible'
  }

  function sendFocus(chatId: string) {
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify({ type: 'focus', focused: _currentFocused(chatId) }))
  }

  function connectWs(chatId: string) {
    if (sockets.value[chatId]) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/chat/${chatId}`)
    sockets.value[chatId] = ws
    lastChatFrameAt[chatId] = nowMs()

    ws.onopen = () => {
      if (toRaw(sockets.value[chatId]) !== ws) return
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
      if (event.type === 'keepalive') return
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
      await loadMessages(chatId)
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
    async function resumeActiveChat() {
      const chatId = activeChatId.value
      if (chatId) {
        await reloadAndReconnectChat(chatId)
      }
      const ews = eventsSocket.value
      if (ews && ews.readyState === WebSocket.OPEN) {
        try { ews.close() } catch { /* ignore */ }
      } else if (!ews) {
        connectEventsWs()
      }
    }

    // Liveness watchdog: cheap timer, only acts on genuinely stale sockets.
    window.setInterval(checkWsLiveness, WS_LIVENESS_CHECK_MS)

    document.addEventListener('visibilitychange', () => {
      documentVisible.value = document.visibilityState === 'visible'
      if (document.visibilityState === 'visible') {
        // iOS Safari / WKWebView suspends JS and sockets when the PWA
        // is backgrounded (screen lock, home button). On resume the
        // WebSockets can be silently dead — `readyState` may still
        // report OPEN, but no messages flow.
        void resumeActiveChat()
        void syncLatest()
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
    serverRestartMessage.value = (message && message.trim()) || DEFAULT_RESTART_MESSAGE
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
        api.get<ChatInfo[]>('/api/chats').then(c => { chats.value = c }).catch(() => { /* ignore */ })
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
        api.get<ChatInfo[]>('/api/chats').then(c => { chats.value = c }).catch(() => { /* ignore */ })
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
        try {
          navigator.serviceWorker?.controller?.postMessage({
            type: 'chat-focused',
            chat_id: msg.chat_id,
          })
        } catch { /* ignore */ }
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
        if (providerSubchats.value[msg.chat_id]) {
          const list = providerSubchats.value[msg.chat_id] || []
          for (const sc of list) {
            delete providerSubchatEvents.value[sc.subchat_id]
          }
          delete providerSubchats.value[msg.chat_id]
        }
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
      case 'provider_subchat_created': {
        const list = providerSubchats.value[msg.parent_chat_id] || []
        if (!list.some(r => r.subchat_id === msg.subchat_id)) {
          list.push(msg.record)
          providerSubchats.value[msg.parent_chat_id] = list
        }
        break
      }
      case 'provider_subchat_status': {
        const list = providerSubchats.value[msg.parent_chat_id] || []
        const idx = list.findIndex(r => r.subchat_id === msg.subchat_id)
        if (idx !== -1) {
          list[idx] = msg.record
        } else {
          list.push(msg.record)
        }
        providerSubchats.value[msg.parent_chat_id] = [...list]
        break
      }
      case 'provider_subchat_event': {
        const events = providerSubchatEvents.value[msg.subchat_id] || []
        events.push(msg.event)
        providerSubchatEvents.value[msg.subchat_id] = [...events]
        // Record metrics (status, token/message counts) arrive via
        // `provider_subchat_status`; there is no need to re-fetch the whole
        // list on every streamed event, which would flood the backend during
        // active streaming.
        break
      }
      case 'provider_subchat_deleted': {
        if (providerSubchats.value[msg.parent_chat_id]) {
          providerSubchats.value[msg.parent_chat_id] = providerSubchats.value[msg.parent_chat_id].filter(
            r => r.subchat_id !== msg.subchat_id
          )
        }
        delete providerSubchatEvents.value[msg.subchat_id]
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
      case 'gws_health': {
        // A Google Workspace login went dead (revoked/expired token). The
        // server debounces to one event per breakage; surface it as a
        // persistent error toast whose "Fix" seeds a chat that can drive the
        // server-managed re-login. The PWA push/menu-bar banner is the other
        // channel (see push.py); this is the live in-app signal.
        pushErrorToast(msg.title || 'Google Workspace login needs attention', msg.body || '')
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

  function sendMessage(chatId: string, text: string, mode: 'queue' | 'steer' = 'queue') {
    const ws = sockets.value[chatId]
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      connectWs(chatId)
      setTimeout(() => sendMessage(chatId, text, mode), 500)
      return
    }
    // Any send implicitly answers (or dismisses) a pending AskUserQuestion
    // picker — the model already got an empty tool result and is reading
    // this turn for the actual answer. Clear the local chat's persisted
    // pending_question too, so a loadMessages racing this send (WS reconnect,
    // reconciliation) doesn't rebuild the picker from a now-stale value.
    if (activeQuestions.value[chatId]) {
      markResolvedQuestion(chatId)
      delete activeQuestions.value[chatId]
    }
    const answeredChat = chats.value.find(c => c.chat_id === chatId)
    if (answeredChat?.pending_question) answeredChat.pending_question = ''
    const chatImages = getPendingBucket(pendingImagesByChat.value, chatId)
    const chatFileComments = getPendingBucket(pendingCommentsByChat.value, chatId)
    const chatComments = getPendingBucket(pendingChatCommentsByChat.value, chatId)
    // Collect images from pendingImages plus any images attached to comments.
    const allImages = new Set<string>(chatImages)
    for (const c of chatFileComments) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    for (const c of chatComments) {
      if (c.images) c.images.forEach(img => allImages.add(img))
    }
    const imageRefs = allImages.size > 0 ? Array.from(allImages) : undefined
    const fileBlock = formatPendingComments(chatFileComments)
    const chatBlock = formatPendingChatComments(chatComments)
    const hasFile = fileBlock.length > 0
    const hasChat = chatBlock.length > 0
    const hasTyped = text.trim().length > 0
    // Reference blocks (quoted text + note) go FIRST, then the typed prompt,
    // so the model reads the material being discussed before the instruction
    // (Anthropic: placing the query at the end of the input improves quality).
    let reference = ''
    if (hasFile) reference += fileBlock
    if (hasChat) reference += (reference ? '\n' : '') + chatBlock
    let composed = reference
    if (hasTyped) composed += (composed ? '\n\n' : '') + text.trim()
    const alreadyStreaming = isChatStreaming(chatId)

    if (alreadyStreaming) {
      // Queue or steer: don't push to the main messages list yet. Queued
      // messages live in queuedMessages until the server echoes them; steered
      // ones arrive as a `steered` event which we fold into messages.
      let queueId: string | undefined
      if (mode === 'queue') {
        if (!queuedMessages.value[chatId]) queuedMessages.value[chatId] = []
        queueId = makeQueuedId()
        queuedMessages.value[chatId].push({ id: queueId, text: composed, images: imageRefs })
      }
      const payload: Record<string, unknown> = { type: 'message', text: composed, mode }
      if (imageRefs) payload.images = imageRefs
      if (queueId) payload.entry_id = queueId
      ws.send(JSON.stringify(payload))
      setPendingBucket<string>(pendingImagesByChat.value, chatId, [])
      persistPendingImages()
      // Remove sent file comments from the durable store so they don't
      // linger in the viewer after the message has been dispatched.
      for (const c of chatFileComments) {
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
      return
    }

    const msgs = messages.value[chatId] || []
    msgs.push({
      role: 'user',
      content: composed,
      timestamp: new Date().toISOString(),
      images: imageRefs,
    })
    messages.value[chatId] = msgs
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
    ws.send(JSON.stringify(payload))
    setPendingBucket<string>(pendingImagesByChat.value, chatId, [])
    persistPendingImages()
    // Remove sent file comments from the durable store so they don't
    // linger in the viewer after the message has been dispatched.
    for (const c of chatFileComments) {
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
    }
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

  // ── Voice ───────────────────────────────────────────────────────────

  async function transcribeVoice(chatId: string, audioBlob: Blob): Promise<string> {
    const form = new FormData()
    form.append('audio', audioBlob, 'voice.webm')
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
    const existing = getPendingBucket(pendingImagesByChat.value, chatId)
    setPendingBucket(pendingImagesByChat.value, chatId, [...existing, ...refs])
    persistPendingImages()
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
    persistPinnedFiles()
  }
  function unpinFile(id: string): void {
    const next = { ...pinnedFilePaths.value }
    delete next[id]
    pinnedFilePaths.value = next
    persistPinnedFiles()
  }
  function pinnedFileFor(id: string): string | undefined {
    return pinnedFilePaths.value[id]
  }

  // ── Pending chat comments ─────────────────────────────────────────
  function addPendingChatComment(c: { selection: string; comment: string; images?: string[] }): string {
    const id = (typeof crypto !== 'undefined' && 'randomUUID' in crypto)
      ? (crypto as { randomUUID: () => string }).randomUUID()
      : `cc_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
    if (activeChatId.value) {
      const existing = getPendingBucket(pendingChatCommentsByChat.value, activeChatId.value)
      setPendingBucket(pendingChatCommentsByChat.value, activeChatId.value, [...existing, { id, selection: c.selection, comment: c.comment, images: c.images }])
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
    pendingChatComments.value[idx] = { ...pendingChatComments.value[idx], comment }
    persistPendingChatComments()
  }
  function addPendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    if (!existing.includes(imageRef)) {
      pendingChatComments.value[idx] = { ...pendingChatComments.value[idx], images: [...existing, imageRef] }
      persistPendingChatComments()
    }
  }
  function removePendingChatCommentImage(id: string, imageRef: string): void {
    const idx = pendingChatComments.value.findIndex(c => c.id === id)
    if (idx === -1) return
    const existing = pendingChatComments.value[idx].images || []
    const next = existing.filter(img => img !== imageRef)
    pendingChatComments.value[idx] = { ...pendingChatComments.value[idx], images: next.length ? next : undefined }
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
        pendingComments.value[pIdx] = { ...pendingComments.value[pIdx], images: [...existing, imageRef] }
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
      pendingComments.value[pIdx] = { ...pendingComments.value[pIdx], images: nextImages.length ? nextImages : undefined }
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

  function _pushFileCard(
    chatId: string,
    payload: { file_path: string; action: string; tool: string },
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
    })
  }

  // Collaboration-friendly, previewable artifacts worth auto-surfacing. Kept
  // deliberately narrow: .md/.csv are the formats the pinned panel renders as
  // an editable, comment-able canvas. Images/pdf/pptx preview too but are
  // rarely something the user wants yanked open unprompted.
  const _AUTO_PIN_EXT_RE = /\.(md|markdown|csv)$/i
  // Bookkeeping files the agent writes as a side effect (memory, proposals,
  // agent config) are not deliverables — surfacing them on every write is noise.
  const _AUTO_PIN_SKIP_BASENAMES = new Set([
    'memory.md', 'user.md', 'memory.local.md',
    'memory-proposals.md', 'learnings.md', 'agents.md', 'claude.md',
  ])

  // Auto-surface a freshly written .md/.csv in the pinned side panel so the
  // user sees a deliverable next to the chat without hunting for it. Fills the
  // empty state only — never yanks a file the user already pinned — and only on
  // desktop, where the split layout exists (mirrors FileViewerModal's canPin
  // gate). localStorage-backed like every other pin; no backend state.
  function _maybeAutoPin(
    chatId: string,
    touches: Array<{ file_path?: string; action?: string }>,
  ): void {
    if (typeof window === 'undefined' || window.innerWidth <= 768) return
    if (pinnedFileFor(chatId)) return
    // Freshest qualifying artifact wins (last touch in the batch).
    for (let i = touches.length - 1; i >= 0; i--) {
      const raw = touches[i]?.file_path
      if (!raw) continue
      const clean = raw.replace(/:\d+$/, '')
      if (!_AUTO_PIN_EXT_RE.test(clean)) continue
      const base = clean.split(/[\\/]/).pop()?.toLowerCase() || ''
      if (_AUTO_PIN_SKIP_BASENAMES.has(base)) continue
      if (!isPlausibleFilePath(raw)) continue
      pinFile(chatId, raw)
      return
    }
  }

  function _flushTimeline(chatId: string): StreamEntry[] {
    const entries = streamingTimeline.value[chatId] || []
    streamingTimeline.value[chatId] = []
    return entries
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
            // do reflect the implied streaming state and clear queue chips.
            if (queuedMessages.value[chatId]?.length) clearQueued(chatId)
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
              upgraded = true
              break
            }
            if (m.role === 'assistant') sawAssistant = true
          }
          if (upgraded) {
            if (queuedMessages.value[chatId]?.length) clearQueued(chatId)
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
        })
        messages.value[chatId] = normalizeMessages([...msgs])
        // The server echoes the flushed queue as one combined user_echo. Clear
        // the local queue chips once we see them as a real user bubble.
        if (queuedMessages.value[chatId]?.length) {
          clearQueued(chatId)
        }
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

      case 'steered': {
        // Steered messages enter the current turn immediately as a user bubble.
        _commitStreamingTextToTimeline(chatId)
        const trimmed = (event.text || '').trim()
        if (!trimmed) break
        const last = msgs[msgs.length - 1]
        if (!(last && last.role === 'user' && last.content === trimmed)) {
          msgs.push({
            role: 'user',
            content: trimmed,
            timestamp: new Date().toISOString(),
            images: event.images?.length ? event.images : undefined,
          })
          messages.value[chatId] = normalizeMessages([...msgs])
          persistMessages()
        }
        break
      }

      case 'text_delta':
        // Visible text starts: any pending thinking block has ended, lock it
        // into the timeline so the Reasoning bubble renders it after the turn.
        _commitStreamingThinkingToTimeline(chatId)
        // Codex starts a new agent-message item when it moves from progress
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
            })
          }
          _maybeAutoPin(chatId, touches)
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

      case 'thinking':
        // Accumulate into the thinking buffer. Committed to the timeline
        // when the model switches to visible text or fires a tool_use
        // (those signal the end of this thinking block). For Anthropic
        // models thinking is usually short and the buffer flushes within
        // the same turn; for Ollama-routed models (Kimi K2.6 etc.) the
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
        const isAllowedRateLimit = message.includes('Rate limit: allowed') && !message.includes('allowed_warning')
        if (message && !ephemeral.has(message) && !message.startsWith('error:') && !isAllowedRateLimit) {
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
        // A completed/interrupted Codex turn may legitimately end after a
        // commentary item with no final answer. Keep that text in the trace;
        // never promote it into the response bubble via the defensive merge.
        if (streamingTextPhase.value[chatId] === 'commentary') {
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
        if (st && !text.includes(st)) {
          if (st.includes(text)) {
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
    projects, chats, workspaces, workspaceProviderOptions, workspaceClaudeAiConnectors, workspaceAppDefaultModel, activeWorkspace, activeChatId, bootstrapped, messages, subagents, providerSubchats, providerSubchatEvents, unread,
    streaming, streamingText, streamingThinking, pendingImages, pendingComments, pendingChatComments, fileComments, queuedMessages,
    projectStreaming, backgroundAgents, toasts, pendingPermissions, activeQuestions, creatingChatProjectIds,
    serverRestarting, serverRestartMessage,
    // Computed
    workspaceProjects, workspaceOptions, activeChat, activeProject, activeMessages, activeSubagents,
    isStreaming, currentStreamingText, currentStreamingThinking, currentQueued, activeBackgroundAgents, currentActivity, currentTimeline, currentLiveUsage, currentStreamStartedAt, projectChats,
    chatUnread, chatNeedsInput, projectNeedsInput, projectUnread, workspaceUnread, totalUnread, clearUnread, markRead, markAllRead,
    recentChats, projectIsStreaming, isChatStreaming, chatHasBackgroundAgents, workspaceIsStreaming, projectFor,
    // Actions
    fetchAll, fetchWorkspaces, createWorkspace, updateWorkspace, deleteWorkspace,
    createProject, updateProject, deleteProject, completeProject,
    fetchCompletedProjects, restoreProject,
    createChat, renameChat, updateChat, handoverChat, forkChat, moveChat, deleteChat, archiveChat, continueArchivedChat, newSession,
    setChatRetry, stopChatRetry, tryChatRetryNow,
    switchChat, switchWorkspace, openChatFromDeepLink,
    syncLatest,
    sendMessage, stopChat, respondPermission, respondQuestion, markResolvedQuestion, transcribeVoice, speakMessage, uploadImages, uploadImageRefs, removePendingImage, clearPendingImages,
    addPendingComment, removePendingComment, clearPendingComments,
    addPendingChatComment, removePendingChatComment, clearPendingChatComments, updatePendingChatComment,
    addPendingChatCommentImage, removePendingChatCommentImage,
    addFileCommentImage, removeFileCommentImage,
    fileCommentsFor, removeFileComment, updateFileComment,
    pinFile, unpinFile, pinnedFileFor,
    removeQueued, removeQueuedById, reorderQueued, editQueued, clearQueued,
    loadMessages, loadSubagents, loadProviderSubchats, loadProviderSubchatEvents,
    connectWs, disconnectWs, connectEventsWs,
    beginServerRestart,
    pushToast, pushErrorToast, dismissToast, fixError,
  }
})
