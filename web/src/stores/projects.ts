import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { api } from '../lib/api'
import { getPendingBucket, normalizePendingBuckets, setPendingBucket } from '../lib/pendingBuckets'
import { buildFixPrompt } from '../lib/fixError'
import type {
  ProjectInfo,
  ChatInfo,
  ChatMessage,
  SubagentTranscript,
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
  // Formatted as plain text "Referring to: ..." rather than XML blocks.
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
  // Locally-tracked queued user messages (sent while a response was already
  // streaming). Cleared when the server echoes them back as a user_echo at
  // flush time, or on result when the queue ends up empty.
  const queuedMessages = ref<Record<string, { text: string; images?: string[] }[]>>({})
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
      return parsed.questions.map((q: Record<string, unknown>, index: number) => ({
        id: String(q.id ?? index),
        question: String(q.question ?? ''),
        header: String(q.header ?? ''),
        multiSelect: Boolean(q.multiSelect),
        allowOther: q.isOther === undefined
          ? true
          : Boolean(q.isOther) || !Array.isArray(q.options) || q.options.length === 0,
        isSecret: Boolean(q.isSecret),
        requestId: resolvedRequestId,
        options: Array.isArray(q.options)
          ? (q.options as Array<Record<string, unknown>>).map(o => ({
              label: String(o.label ?? ''),
              description: o.description ? String(o.description) : '',
            }))
          : [],
      }))
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
    if (qs.length) activeQuestions.value[chatId] = qs
  }
  // Per-chat map from a Task/Agent dispatch's tool_use_id to a short subagent
  // label like "[Explore]". Populated when we see the parent's Task tool_use,
  // consumed when later stream events from inside the subagent arrive with
  // `parent_tool_use_id` set so we can prefix the activity line in the trace.
  // Cleared when the result event closes the turn — labels are turn-scoped.
  const subagentLabels = ref<Record<string, Record<string, string>>>({})
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
  // every STREAM_KEEPALIVE_SECONDS (15s, see ciao/web/chat_broker.py). We use
  // those frames purely as a liveness signal: a socket that reports
  // readyState OPEN but has received nothing for well over the keepalive
  // cadence is half-open (common after iOS/WKWebView suspend or a flaky
  // network) and will never fire `onclose`, so results/subagent events
  // published server-side never arrive and the UI looks hung until the user
  // sends a message. The watchdog below force-reconnects such sockets.
  const WS_STALE_MS = 45000 // ~3 missed keepalives
  const WS_LIVENESS_CHECK_MS = 10000
  let lastEventsFrameAt = 0
  const lastChatFrameAt: Record<string, number> = {}
  const nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now())

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

  const isStreaming = computed(() =>
    streaming.value[activeChatId.value || ''] || false
  )

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
        if (message.tool_name === '_filecard') return Boolean(message.file_path)
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
      chatMessages.map((message) => ({
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
  function mergeMetadata(server: ChatMessage[], local: ChatMessage[]): ChatMessage[] {
    return server.map((sMsg, i) => {
      const lMsg = local[i]
      if (!lMsg || lMsg.role !== sMsg.role || lMsg.content !== sMsg.content) {
        return sMsg
      }
      const merged: ChatMessage = { ...sMsg }
      if (lMsg.usage && !sMsg.usage) merged.usage = lMsg.usage
      if (lMsg.quota && !sMsg.quota) merged.quota = lMsg.quota
      if (lMsg.effective_model && !sMsg.effective_model) merged.effective_model = lMsg.effective_model
      if (lMsg.is_error !== undefined && sMsg.is_error === undefined) merged.is_error = lMsg.is_error
      if (lMsg.turn_index != null && sMsg.turn_index == null) merged.turn_index = lMsg.turn_index
      if (lMsg.duration_ms != null && sMsg.duration_ms == null) merged.duration_ms = lMsg.duration_ms
      // Prefer a non-empty timestamp from either side: the server-supplied
      // sent_at wins (authoritative across reloads); fall back to whatever
      // the streaming handler stamped locally.
      if (!merged.timestamp && lMsg.timestamp) merged.timestamp = lMsg.timestamp
      return merged
    })
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
    delete projectStreaming.value[chatId]
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
      if (streaming.value[chatId] && !queuedMessages.value[chatId]?.length && hasSettledHistory(chatId)) {
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
      const normalizedLocal = normalizeMessages(messages.value[chatId] || [])

      if (historySignature(normalizedServer) !== historySignature(normalizedLocal)) {
        // Guard: never replace a longer local history with a shorter server
        // history. This can happen when the SDK session was reset (e.g. resume
        // failure caused a fresh session) and the new session file has fewer
        // messages than the frontend accumulated from streaming events.
        if (normalizedServer.length < normalizedLocal.length) {
          console.warn(
            `[loadMessages] Server returned ${normalizedServer.length} messages but local has ${normalizedLocal.length}; keeping local to avoid data loss`,
          )
          return
        }
        messages.value[chatId] = mergeMetadata(normalizedServer, normalizedLocal)
        persistMessages()
      } else if (historySignature(normalizedLocal) !== historySignature(messages.value[chatId] || [])) {
        messages.value[chatId] = normalizedLocal
        persistMessages()
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
        void loadSubagents(chatId)
        return
      }
      if (last.role === 'system' && last.tool_name !== '_activity') {
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

  async function switchWorkspace(ws: WorkspaceName) {
    if (activeWorkspace.value === ws) return
    if (activeChatId.value) disconnectWs(activeChatId.value)
    activeWorkspace.value = ws
    persistState()
    await transitionToFirstChat()
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
      lastChatFrameAt[chatId] = nowMs()
      sendFocus(chatId)
    }

    ws.onmessage = (ev) => {
      // Any frame (including the server keepalive) proves the socket is live.
      lastChatFrameAt[chatId] = nowMs()
      const event: WsEvent = JSON.parse(ev.data)
      handleEvent(chatId, event)
    }

    ws.onclose = () => {
      delete sockets.value[chatId]
      delete lastChatFrameAt[chatId]
      // Clear local streaming state; the server broker keeps the SDK call
      // running, and any reconnect will replay buffered events so the UI
      // picks up right where it left off.
      streaming.value[chatId] = false
      streamingText.value[chatId] = ''
      streamingThinking.value[chatId] = ''
      streamingTimeline.value[chatId] = []
      delete streamingTextPhase.value[chatId]
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
    disconnectWs(chatId)
    await loadMessages(chatId)
    void loadSubagents(chatId)
    connectWs(chatId)
    void markRead(chatId)
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
    const ws = sockets.value[chatId]
    if (ws) {
      ws.close()
      delete sockets.value[chatId]
    }
  }

  // ── Global events WS (cross-chat awareness) ─────────────────────────

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
      opened = true
      eventsWsFailureStreak = 0
      lastEventsFrameAt = nowMs()
    }

    ws.onmessage = (ev) => {
      // Any frame (including the server keepalive) proves the socket is live.
      lastEventsFrameAt = nowMs()
      let msg: EventsWsMessage
      try { msg = JSON.parse(ev.data) } catch { return }
      handleEventsMessage(msg)
    }

    ws.onclose = () => {
      eventsSocket.value = null
      if (opened) {
        eventsWsFailureStreak = 0
      } else {
        eventsWsFailureStreak += 1
        if (eventsWsFailureStreak === 5) {
          // Likely an auth rejection: probe the HTTP API so its 401
          // handling can redirect this stale tab to /login.
          void api.get('/api/projects').catch(() => {})
        }
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
        // announcement (or a subagent spawning children): just update the
        // badge silently. Only a drop means an agent finished and produced
        // output worth surfacing.
        if (msg.remaining >= prevAgents && msg.remaining > 0) break
        const isFocused = activeChatId.value === msg.chat_id &&
          (typeof document === 'undefined' || document.visibilityState === 'visible')
        if (isFocused) {
          // Subagent transcripts land after the parent turn's result. Refresh
          // history and the subagent panel so the user sees the update without
          // having to switch chats or wait for the next sync interval.
          void reconcileAfterResult(msg.chat_id)
          void loadSubagents(msg.chat_id)
        } else {
          unread.value[msg.chat_id] = 1
          persistUnread()
          if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
            pushToast({
              chat_id: msg.chat_id,
              title: 'ciaobot',
              body: msg.remaining === 0 ? 'Background agents finished' : 'Background agent update',
            })
          }
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
        if (streaming.value[msg.chat_id]) delete streaming.value[msg.chat_id]
        if (streamingText.value[msg.chat_id]) delete streamingText.value[msg.chat_id]
        delete streamingTextPhase.value[msg.chat_id]
        if (queuedMessages.value[msg.chat_id]) delete queuedMessages.value[msg.chat_id]
        if (unread.value[msg.chat_id]) {
          delete unread.value[msg.chat_id]
          persistUnread()
        }
        const ws = sockets.value[msg.chat_id]
        if (ws) {
          try { ws.close() } catch { /* ignore */ }
          delete sockets.value[msg.chat_id]
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
    }
  }

  // ── Send messages ───────────────────────────────────────────────────

  // Render pendingComments as a clean block prefixed to the user's typed text.
  // The model sees the file, line range, selected text, and comment in a
  // readable markdown-style format that is also pleasant to read in the chat
  // bubble. No XML tags.
  function formatPendingComments(comments = pendingComments.value): string {
    if (!comments.length) return ''
    const blocks = comments.map((c, i) => {
      const parts: string[] = [`--- Comment ${i + 1} on ${c.path} ---`]
      if (c.lineStart) {
        const range = c.lineEnd && c.lineEnd !== c.lineStart
          ? `lines ${c.lineStart}-${c.lineEnd}`
          : `line ${c.lineStart}`
        parts.push(`(${range})`)
      }
      parts.push(
        '',
        'Selected:',
        `> ${c.selection}`,
        '',
        'Comment:',
        c.comment,
      )
      if (c.images?.length) {
        parts.push('', 'Attachments:')
        c.images.forEach((img, idx) => {
          parts.push(`[Image ${idx + 1}] ${img}`)
        })
      }
      return parts.join('\n')
    })
    return blocks.join('\n\n')
  }

  function formatPendingChatComments(comments = pendingChatComments.value): string {
    if (!comments.length) return ''
    const lines = comments.map((c) => {
      return `Referring to: "${c.selection}"\nmy comment is: "${c.comment}"`
    })
    return lines.join('\n\n')
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
    // Typed text goes first; attachments (file + chat comments) follow after a
    // `----` separator so the model reads the user's prompt before context.
    let composed = ''
    if (hasTyped) composed += text.trim()
    let attachments = ''
    if (hasFile) attachments += fileBlock
    if (hasChat) attachments += (attachments ? '\n\n' : '') + chatBlock
    if (attachments) {
      composed += (composed ? '\n\n----\n\n' : '') + attachments
    }
    const alreadyStreaming = isChatStreaming(chatId)

    if (alreadyStreaming) {
      // Queue or steer: don't push to the main messages list yet. Queued
      // messages live in queuedMessages until the server echoes them; steered
      // ones arrive as a `steered` event which we fold into messages.
      if (mode === 'queue') {
        if (!queuedMessages.value[chatId]) queuedMessages.value[chatId] = []
        queuedMessages.value[chatId].push({ text: composed, images: imageRefs })
      }
      const payload: Record<string, unknown> = { type: 'message', text: composed, mode }
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
    list.splice(index, 1)
    if (!list.length) delete queuedMessages.value[chatId]
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
    if (!streamingTimeline.value[chatId]) streamingTimeline.value[chatId] = []
    streamingTimeline.value[chatId].push({
      kind: 'filecard',
      content: payload.file_path,
      file_path: payload.file_path,
      action: payload.action,
      tool: payload.tool,
    })
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
          // assigned turn_index yet — upgrade it in place instead of pushing
          // a duplicate.
          let upgraded = false
          for (let i = msgs.length - 1; i >= 0; i--) {
            const m = msgs[i]
            if (m.role === 'user' && m.turn_index == null && m.content === trimmed) {
              m.turn_index = turnIndex
              upgraded = true
              break
            }
            // Don't walk past the boundary of this turn — an assistant reply
            // ends the previous turn, so we only scan back through the tail.
            if (m.role === 'assistant') break
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
        // locally for optimistic rendering, skip. Otherwise (e.g. another
        // client queued it), add it so chips stay consistent.
        const trimmed = (event.text || '').trim()
        if (!trimmed) break
        // Defensive: if a user bubble with matching content is already in the
        // timeline, the queue was already flushed. This catches buffer-replay
        // scenarios (WS reconnect, post-reload subscribe) where a stale
        // `queued` event arrives after `loadMessages` hydrated the real bubble
        // but the matching `user_echo` is no longer in the broker buffer to
        // clear the chip. Without this guard the chip sticks forever.
        if (queuedTextAlreadyRendered(msgs, trimmed)) break
        const list = queuedMessages.value[chatId] || []
        if (!list.some(q => q.text === trimmed)) {
          list.push({ text: trimmed, images: event.images?.length ? event.images : undefined })
          queuedMessages.value[chatId] = list
        }
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

        // File-mutating tool calls (Write/Edit/MultiEdit/NotebookEdit) get
        // their own inline preview card. Backend tags these with `file_touch`
        // in chat_broker.event_to_json. Subagent file writes also get a card,
        // with the dispatch label preserved in the `tool` field for context.
        if (event.file_touch?.file_path) {
          _pushFileCard(chatId, {
            file_path: event.file_touch.file_path,
            action: event.file_touch.action || 'touched',
            tool: event.tool_name,
          })
          break
        }

        // Record Task/Agent dispatches so we can prefix any tool calls that
        // fire from inside the subagent (those arrive later with
        // parent_tool_use_id pointing back at this dispatch's tool_use_id).
        // Server-side _summarize_tool_input already formats the input as
        // "[subagent_type] description"; we only need the bracketed prefix.
        const isSubagentDispatch =
          (event.tool_name === 'Task' || event.tool_name === 'Agent')
          && !!event.tool_use_id
        if (isSubagentDispatch) {
          const match = (event.tool_input || '').match(/^\[([^\]]+)\]/)
          const label = match ? `[${match[1]}]` : '[subagent]'
          if (!subagentLabels.value[chatId]) subagentLabels.value[chatId] = {}
          subagentLabels.value[chatId][event.tool_use_id!] = label
        }

        let line = event.tool_input
          ? `${_toolIcon(event.tool_name)} ${event.tool_name} ${event.tool_input}`
          : `${_toolIcon(event.tool_name)} ${event.tool_name}`

        // Subagent activity: prefix with the parent dispatch's label and a
        // turnstile arrow so the trace reads top-down as
        //   🤖 Agent [Explore] Trace WebSocket …
        //     ↳ [Explore] $ Bash find …
        // Falls back to a generic [subagent] tag if the dispatch's label
        // isn't in the map (e.g. WS reconnect after a buffer drop).
        if (event.parent_tool_use_id) {
          const label = subagentLabels.value[chatId]?.[event.parent_tool_use_id]
            ?? '[subagent]'
          line = `↳ ${label} ${line}`
        }

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

      case 'status':
        break

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
        // Turn ended: the server has already resolved any still-pending gate
        // futures as deny via cancel_all(). Drop the bubbles on our side too
        // so a late click can't race a brand-new turn.
        delete pendingPermissions.value[chatId]
        // Subagent labels are turn-scoped — a tool_use_id from this turn
        // can't possibly match a future dispatch's id, but clearing the map
        // keeps memory bounded for long-lived chats with many turns.
        delete subagentLabels.value[chatId]
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
        delete pendingPermissions.value[chatId]
        persistMessages()
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
    projects, chats, workspaces, workspaceProviderOptions, workspaceClaudeAiConnectors, workspaceAppDefaultModel, activeWorkspace, activeChatId, bootstrapped, messages, subagents, unread,
    streaming, streamingText, streamingThinking, pendingImages, pendingComments, pendingChatComments, fileComments, queuedMessages,
    projectStreaming, backgroundAgents, toasts, pendingPermissions, activeQuestions, creatingChatProjectIds,
    // Computed
    workspaceProjects, workspaceOptions, activeChat, activeProject, activeMessages, activeSubagents,
    isStreaming, currentStreamingText, currentStreamingThinking, currentQueued, activeBackgroundAgents, currentActivity, currentTimeline, currentLiveUsage, currentStreamStartedAt, projectChats,
    chatUnread, chatNeedsInput, projectNeedsInput, projectUnread, workspaceUnread, totalUnread, clearUnread, markRead, markAllRead,
    recentChats, projectIsStreaming, isChatStreaming, chatHasBackgroundAgents, workspaceIsStreaming, projectFor,
    // Actions
    fetchAll, fetchWorkspaces, createWorkspace, updateWorkspace, deleteWorkspace,
    createProject, updateProject, deleteProject, completeProject,
    fetchCompletedProjects, restoreProject,
    createChat, renameChat, updateChat, handoverChat, moveChat, deleteChat, archiveChat, continueArchivedChat, newSession,
    setChatRetry, stopChatRetry, tryChatRetryNow,
    switchChat, switchWorkspace, openChatFromDeepLink,
    syncLatest,
    sendMessage, stopChat, respondPermission, respondQuestion, transcribeVoice, speakMessage, uploadImages, uploadImageRefs, removePendingImage, clearPendingImages,
    addPendingComment, removePendingComment, clearPendingComments,
    addPendingChatComment, removePendingChatComment, clearPendingChatComments, updatePendingChatComment,
    addPendingChatCommentImage, removePendingChatCommentImage,
    addFileCommentImage, removeFileCommentImage,
    fileCommentsFor, removeFileComment, updateFileComment,
    pinFile, unpinFile, pinnedFileFor,
    removeQueued, clearQueued,
    loadMessages, loadSubagents,
    connectWs, disconnectWs, connectEventsWs,
    pushToast, pushErrorToast, dismissToast, fixError,
  }
})
