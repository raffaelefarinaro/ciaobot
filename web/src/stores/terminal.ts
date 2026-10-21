import { defineStore } from 'pinia'
import { onScopeDispose, ref } from 'vue'
import { api } from '../lib/api'
import {
  TERMINAL_INPUT_MAX_BYTES,
  TERMINAL_MAX_COLS,
  TERMINAL_MAX_FOLDER_CHARS,
  TERMINAL_MAX_ROWS,
  TERMINAL_MAX_SESSION_ID_CHARS,
  TERMINAL_MIN_COLS,
  TERMINAL_MIN_ROWS,
  encodeTerminalBytes,
  parseTerminalServerFrame,
  terminalCloseReason,
  terminalOutputSlice,
  terminalReconnectDelayMs,
  type TerminalClientFrame,
  type TerminalServerFrame,
  type TerminalSessionInfo,
} from '../lib/terminalProtocol'

// The browser half of the embedded terminal: one owner for the HTTP actions,
// the socket lifecycle, the bounded renderer backpressure and the replay
// offsets.
//
// The engine half of this feature is not in this repository yet (the other
// children of #1680 own it), so this store is written against the pinned wire
// contract and nothing here pretends a backend exists: no entry point imports
// it, and no user-facing control calls it yet.
//
// Two rules shape everything below.
//
// The grant is a cookie. A terminal grant is a random bearer in an HttpOnly
// cookie, so the browser never holds it: there is no token in this file, no
// token in a ref, and every request rides the same-origin session the api
// helper already uses. The password is passed to one POST and is not kept.
//
// The offset belongs to the renderer, not to the socket. `ready.seq` says how
// many bytes exist; it is never the number of bytes the client consumed. The
// cursor advances only after the renderer's write promise resolves, so a
// reconnect resumes from the screen the user actually has.

export type { TerminalSessionInfo }

/** What a terminal is scoped to: one chat, or one project. */
export type TerminalTarget = { chatId: string } | { projectId: string }

/** Where the terminal surface is in its lifecycle. */
export type TerminalPhase =
  | 'closed'
  | 'loading'
  | 'locked'
  | 'ready'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'takeover'
  | 'ended'
  | 'error'

/** The engine's answer to "can this browser open a terminal, and until when?" */
export interface TerminalStatus {
  available: boolean
  reason: string
  platform: string
  host: string
  /** Seconds left on the terminal grant; zero means the pane is locked. */
  unlock_expires_in: number
  detached_ttl_seconds: number
}

/**
 * The surface that renders terminal bytes.
 *
 * `write` resolves when the renderer has taken the bytes; that resolution is
 * what advances the consumed offset. `reset` clears the screen before a
 * truncated replay is written over it.
 */
export interface TerminalRenderer {
  sessionId: string
  write(bytes: Uint8Array): Promise<void>
  reset(): void
}

const TERMINAL_API = '/api/terminal'
/** Output the client may hold while the renderer catches up, in bytes. */
const PENDING_OUTPUT_MAX_BYTES = 1024 * 1024
/** Bytes the client may have queued towards the socket before it refuses input. */
const SEND_BUFFER_MAX_BYTES = 256 * 1024
/** How often a live socket is pinged. */
const PING_INTERVAL_MS = 15_000
/** Silence after which the connection is treated as stalled, in milliseconds. */
const WATCHDOG_MS = 30_000
/** Automatic reconnect attempts before the client gives up and asks the user. */
const MAX_AUTO_RECONNECT_ATTEMPTS = 8

// ── Response validation ──────────────────────────────────────────────────
//
// The engine's answers are read through narrow shapes. A response that does
// not fit is refused rather than reinterpreted: a session list that lost a
// field is not a session list with an empty field, and guessing is how a
// terminal ends up writing bytes at an offset the engine never was.

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** A non-negative safe integer: a byte count, a second count, a cell count. */
function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

/** A cell count inside the engine's terminal bounds, never a boolean. */
function isCellCount(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max
}

function parseStatus(value: unknown): TerminalStatus | null {
  if (!isRecord(value)) return null
  const { available, reason, platform, host, unlock_expires_in, detached_ttl_seconds } = value
  if (typeof available !== 'boolean') return null
  if (typeof reason !== 'string' || reason.length > 128) return null
  if (typeof platform !== 'string' || platform.length > 64) return null
  if (typeof host !== 'string' || host.length > 256) return null
  if (!isCount(unlock_expires_in)) return null
  if (!isCount(detached_ttl_seconds)) return null
  return { available, reason, platform, host, unlock_expires_in, detached_ttl_seconds }
}

function parseSessionInfo(value: unknown): TerminalSessionInfo | null {
  if (!isRecord(value)) return null
  const { session_id, folder, cols, rows, seq, attached, exited, exit_code } = value
  if (typeof session_id !== 'string' || !session_id.trim()) return null
  if (session_id.length > TERMINAL_MAX_SESSION_ID_CHARS) return null
  if (typeof folder !== 'string' || folder.length > TERMINAL_MAX_FOLDER_CHARS) return null
  if (!isCellCount(cols, TERMINAL_MIN_COLS, TERMINAL_MAX_COLS)) return null
  if (!isCellCount(rows, TERMINAL_MIN_ROWS, TERMINAL_MAX_ROWS)) return null
  if (!isCount(seq)) return null
  if (typeof attached !== 'boolean' || typeof exited !== 'boolean') return null
  if (exit_code !== null && !(typeof exit_code === 'number' && Number.isSafeInteger(exit_code))) {
    return null
  }
  return { session_id, folder, cols, rows, seq, attached, exited, exit_code }
}

function parseSessionList(value: unknown): TerminalSessionInfo[] | null {
  if (!isRecord(value)) return null
  const { sessions } = value
  if (!Array.isArray(sessions)) return null
  const parsed: TerminalSessionInfo[] = []
  for (const entry of sessions) {
    const session = parseSessionInfo(entry)
    if (!session) return null
    parsed.push(session)
  }
  return parsed
}

function parseSessionEnvelope(value: unknown): TerminalSessionInfo | null {
  if (!isRecord(value)) return null
  return parseSessionInfo(value.session)
}

/** The seconds the grant lasts, or null when the answer is not an unlock. */
function parseUnlock(value: unknown): number | null {
  if (!isRecord(value)) return null
  if (value.ok !== true) return null
  if (!isCount(value.expires_in)) return null
  return value.expires_in
}

function parseOk(value: unknown): boolean {
  return isRecord(value) && value.ok === true
}

/** The scope key: the same scope must not be mistaken for another one. */
function scopeKey(scope: TerminalTarget): string {
  return 'chatId' in scope ? `chat:${scope.chatId}` : `project:${scope.projectId}`
}

/** The one query parameter a session list is scoped by. */
function scopeQuery(scope: TerminalTarget): string {
  const params = new URLSearchParams()
  if ('chatId' in scope) params.set('chat_id', scope.chatId)
  else params.set('project_id', scope.projectId)
  return params.toString()
}

/** The one body field a session creation is scoped by. Never a path, never a command. */
function scopeBody(scope: TerminalTarget): { chat_id: string } | { project_id: string } {
  if ('chatId' in scope) return { chat_id: scope.chatId }
  return { project_id: scope.projectId }
}

export const useTerminalStore = defineStore('terminal', () => {
  // ── State ──────────────────────────────────────────────────────────────

  const visible = ref(false)
  const target = ref<TerminalTarget | null>(null)
  const status = ref<TerminalStatus | null>(null)
  const sessions = ref<TerminalSessionInfo[]>([])
  const activeSession = ref<TerminalSessionInfo | null>(null)
  const phase = ref<TerminalPhase>('closed')
  /** A friendly message for the pane. Never a payload, never a credential. */
  const error = ref('')
  /** A friendly note that is not a failure, such as a truncated replay. */
  const warning = ref('')
  /** True once the engine reported that its retained stream lost bytes. */
  const truncated = ref(false)
  /** True while the terminal grant in the cookie is live. */
  const unlocked = ref(false)
  /** True while a session creation is in flight, so a second click is refused. */
  const creating = ref(false)
  /** True while a session is being ended. */
  const ending = ref(false)
  /** True while an unlock is in flight. */
  const unlocking = ref(false)

  // ── Internals ──────────────────────────────────────────────────────────

  let disposed = false
  /**
   * Bumped when the scope changes, so a reply for the previous scope cannot
   * land on the new one.
   */
  let scopeEpoch = 0
  /**
   * Bumped when the selected session changes, so a reply that would move the
   * selection cannot yank the user back to an older choice.
   */
  let selectionEpoch = 0
  /**
   * Bumped when the renderer changes, so a reply that would move the
   * selection cannot land on a different surface.
   */
  let rendererEpoch = 0
  /**
   * Bumped when the connection is replaced.
   *
   * A pending retry belongs to the connection it was scheduled for: when a
   * fresh unlock or a new socket replaces it, the old retry must not open a
   * second connection over the new one.
   */
  let connectionEpoch = 0
  /** The renderer currently drawing the terminal, if any. */
  let renderer: TerminalRenderer | null = null
  /** The socket currently owned by the store, if any. */
  let socket: WebSocket | null = null
  /**
   * Bytes the renderer has acknowledged — the only offset a resume may use.
   *
   * The contract is explicit: the client applies an offset only after the
   * renderer's write callback completes, so this is the number that goes
   * into an `attach`.
   */
  let consumed = 0
  /**
   * Bytes handed to the renderer's write — what the screen holds.
   *
   * The screen is the authority on what has been shown, so a replay is
   * trimmed against it: the same bytes must not reach the screen twice.
   */
  let screen = 0
  /** Bumped when the stream itself changes, so a late write cannot be counted. */
  let streamEpoch = 0
  /** Automatic reconnect attempts since the last successful handshake. */
  let reconnectAttempts = 0
  /** When the last valid frame arrived, in milliseconds. */
  let lastFrameAt = 0

  /**
   * The acknowledged offset of every renderer this store has seen.
   *
   * A renderer that is detached and attached again keeps its screen, so it
   * resumes where it left off; a renderer the store has never seen starts at
   * zero and takes the retained replay.
   */
  const rendererOffsets = new WeakMap<TerminalRenderer, number>()

  interface PendingChunk {
    bytes: Uint8Array
    /** The offset the stream has reached once these bytes are on the screen. */
    nextOffset: number
    seq: number
  }
  const pending: PendingChunk[] = []
  let pendingBytes = 0
  /** Bumped when the queue is dropped, so a late write cannot advance the cursor. */
  let queueSeq = 0
  let draining = false

  const timers = new Set<ReturnType<typeof setTimeout>>()
  let pingTimer: ReturnType<typeof setInterval> | null = null
  let listenersInstalled = false

  function scheduleTimer(fn: () => void, ms: number): ReturnType<typeof setTimeout> {
    const id = setTimeout(() => {
      timers.delete(id)
      if (!disposed) fn()
    }, ms)
    if (!disposed) timers.add(id)
    return id
  }

  function clearQueue(): void {
    pending.length = 0
    pendingBytes = 0
    queueSeq += 1
  }

  /**
   * Drop the bytes the renderer never acknowledged.
   *
   * The engine replays from the acknowledged offset, so the replayed bytes
   * are trimmed against the ones the *screen* holds: the screen is the
   * authority on what has been shown. The acknowledgement is what the engine
   * replays from, and it only moves when the renderer says so.
   *
   * The stream epoch moves with it: a write still in flight belongs to the
   * stream that is being dropped, so it must not move the cursor of the one
   * that replaces it.
   */
  function dropUnacknowledged(): void {
    clearQueue()
    streamEpoch += 1
  }

  /** Close the socket without letting its close event decide anything. */
  function closeSocket(): void {
    const ws = socket
    socket = null
    connectionEpoch += 1
    if (!ws) return
    ws.onopen = null
    ws.onmessage = null
    ws.onclose = null
    ws.onerror = null
    try {
      ws.close()
    } catch {
      /* a socket that cannot close is a socket we no longer own */
    }
  }

  function stopSocketTimers(): void {
    if (pingTimer !== null) {
      clearInterval(pingTimer)
      pingTimer = null
    }
  }

  /** Drop the socket and its timers. In-flight replies see the epoch bump. */
  function invalidateWorld(): void {
    closeSocket()
    stopSocketTimers()
  }

  function resetStream(): void {
    clearQueue()
    streamEpoch += 1
    consumed = 0
    screen = 0
  }

  function fail(message: string): void {
    error.value = message
    phase.value = 'error'
    closeSocket()
    stopSocketTimers()
  }

  // ── Renderer backpressure ──────────────────────────────────────────────

  /**
   * One drain loop, not one task per frame.
   *
   * The renderer writes asynchronously, so the bytes it has not taken yet
   * wait here. The queue is bounded: past a megabyte the client is slower
   * than the shell, and the socket is closed rather than allowed to grow.
   *
   * The queue is also the ordering guarantee: the replay the engine sends
   * after a reconnect and the bytes still waiting here reach the renderer
   * in stream order, because both go through this one queue.
   */
  async function drain(): Promise<void> {
    if (draining) return
    draining = true
    try {
      while (pending.length > 0) {
        const chunk = pending.shift() as PendingChunk
        const target = renderer
        if (!target) {
          pendingBytes -= chunk.bytes.length
          return
        }
        const myStream = streamEpoch
        try {
          await target.write(chunk.bytes)
        } catch {
          if (chunk.seq === queueSeq) pendingBytes -= chunk.bytes.length
          fail('The terminal view could not be written to.')
          return
        }
        // The renderer took the bytes, so *that* renderer holds them: the
        // record follows the renderer, not the socket.
        rendererOffsets.set(target, chunk.nextOffset)
        // A replaced renderer or a reset stream owns the outcome now: the
        // bytes went to a screen that is no longer the one on show, so the
        // live cursor does not move for them.
        if (renderer !== target || myStream !== streamEpoch) return
        pendingBytes -= chunk.bytes.length
        // The renderer has acknowledged the bytes, so the offset may move.
        consumed = chunk.nextOffset
      }
    } finally {
      draining = false
      if (pending.length > 0 && !disposed) void drain()
    }
  }

  /**
   * Queue the bytes for the renderer.
   *
   * The screen is committed to these bytes from here on — they are in the
   * queue and will be written — so the screen cursor moves now, not when the
   * write promise resolves. The *acknowledged* offset moves later, and only
   * that one goes into an `attach`.
   */
  function enqueueOutput(bytes: Uint8Array, nextOffset: number): void {
    if (bytes.length === 0) return
    if (pendingBytes + bytes.length > PENDING_OUTPUT_MAX_BYTES) {
      fail('The shell is producing output faster than the terminal can show it.')
      return
    }
    pendingBytes += bytes.length
    screen = nextOffset
    pending.push({ bytes, nextOffset, seq: queueSeq })
    void drain()
  }

  // ── Socket ─────────────────────────────────────────────────────────────

  function terminalSocketUrl(sessionId: string): string {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    // The session id flows from server state; one encoded path segment keeps a
    // crafted id from rewriting the rest of the URL. No credential rides the
    // query string: the grant is the cookie.
    return `${proto}//${location.host}/ws/terminal/${encodeURIComponent(sessionId)}`
  }

  function sendFrame(ws: WebSocket, frame: TerminalClientFrame): void {
    if (ws.readyState !== WebSocket.OPEN) return
    ws.send(JSON.stringify(frame))
  }

  /** A socket is owned only while the surface, the scope and the grant agree. */
  function socketEligible(): boolean {
    const current = renderer
    const session = activeSession.value
    if (!visible.value || !unlocked.value) return false
    if (!current || !session) return false
    return current.sessionId === session.session_id
  }

  function maybeConnect(): void {
    if (!socketEligible()) return
    if (socket && socket.readyState <= WebSocket.OPEN) return
    openSocket(false)
  }

  function openSocket(takeover: boolean): void {
    const session = activeSession.value
    const current = renderer
    if (!session || !current) return
    closeSocket()
    // The engine replays from the acknowledged offset, so the bytes the
    // screen holds above it are re-sent. They are trimmed against the
    // screen, and the screen is re-based to the acknowledged offset so the
    // two agree about where the stream starts.
    screen = consumed
    const ws = new WebSocket(terminalSocketUrl(session.session_id))
    socket = ws
    lastFrameAt = Date.now()
    phase.value = 'connecting'

    ws.onopen = () => {
      if (socket !== ws) return
      lastFrameAt = Date.now()
      reconnectAttempts = 0
      phase.value = 'live'
      // The resume point is the offset the renderer acknowledged — never
      // `ready.seq`, which is only a snapshot of what exists.
      sendFrame(ws, { type: 'attach', offset: consumed, takeover })
      startPingTimer()
    }

    ws.onmessage = (event: MessageEvent) => {
      if (socket !== ws) return
      const raw = event.data
      if (typeof raw !== 'string') {
        fail('The terminal sent a frame this app could not read.')
        return
      }
      const frame = parseTerminalServerFrame(raw)
      if (!frame) {
        // A frame that does not parse is not a frame to act on, and guessing
        // would move the offset somewhere the engine never was.
        fail('The terminal sent a frame this app could not read.')
        return
      }
      // Any valid frame proves the socket is alive.
      lastFrameAt = Date.now()
      handleFrame(frame)
    }

    ws.onclose = (event: CloseEvent) => {
      if (socket !== ws) return
      socket = null
      stopSocketTimers()
      handleClose(event?.code)
    }

    ws.onerror = () => {
      if (socket !== ws) return
      if (!error.value) error.value = 'The terminal connection reported an error.'
    }
  }

  function handleFrame(frame: TerminalServerFrame): void {
    const current = renderer
    if (!current) return
    switch (frame.type) {
      case 'ready': {
        if (frame.session_id !== activeSession.value?.session_id) {
          fail('The terminal connected to a different session.')
          return
        }
        // The engine replays from the offset the client acknowledged, so the
        // gap is measured against that offset. A retained buffer that starts
        // after it means the bytes in between are gone.
        if (frame.replay_start > consumed) {
          if (!frame.truncated) {
            // The engine no longer holds the bytes between the client's cursor
            // and its buffer, and does not admit to having lost them: the
            // stream cannot be continued without inventing bytes.
            fail('The terminal skipped output that never reached this browser.')
            return
          }
          // The retained stream starts after the cursor and the engine says
          // it lost the rest: the screen is reset and the replay is written
          // over it. It is a partial stream, not a full-screen recovery.
          dropUnacknowledged()
          current.reset()
          consumed = frame.replay_start
          screen = frame.replay_start
          truncated.value = true
          warning.value =
            'The retained output was truncated, so the terminal may not show the full screen state.'
        }
        if (frame.truncated) truncated.value = true
        phase.value = 'live'
        break
      }
      case 'output': {
        // The replay overlaps the bytes the screen already shows, so the
        // frame is trimmed against them — the screen must not show the same
        // bytes twice. The offset that goes into an attach is a different
        // number: the one the renderer acknowledged.
        //
        // A frame that starts past what this client received is a gap: the
        // slice refuses it, because the bytes in between never arrived and
        // the renderer must not be handed a hole.
        const slice = terminalOutputSlice(frame, screen)
        if (!slice) {
          fail('The terminal sent output this browser never received.')
          return
        }
        if (slice.bytes.length === 0) {
          // The screen already holds these bytes, so the acknowledgement can
          // catch up with the stream: nothing new has to be written.
          consumed = Math.max(consumed, frame.end)
          break
        }
        enqueueOutput(slice.bytes, slice.nextOffset)
        break
      }
      case 'replay_end': {
        // The replay is over. It says how many bytes exist, not how many
        // this client consumed, so the cursor does not move.
        if (frame.truncated) truncated.value = true
        break
      }
      case 'exit': {
        markSessionExited(activeSession.value?.session_id, frame.code)
        phase.value = 'ended'
        error.value = ''
        closeSocket()
        stopSocketTimers()
        break
      }
      case 'pong':
        break
      case 'error': {
        error.value = frame.reason
        phase.value = 'error'
        closeSocket()
        stopSocketTimers()
        break
      }
      default:
        break
    }
  }

  function markSessionExited(sessionId: string | undefined, code: number | null): void {
    if (!sessionId) return
    sessions.value = sessions.value.map((session) =>
      session.session_id === sessionId
        ? { ...session, exited: true, exit_code: code, attached: false }
        : session,
    )
    const current = activeSession.value
    if (current && current.session_id === sessionId) {
      activeSession.value = { ...current, exited: true, exit_code: code, attached: false }
    }
  }

  function handleClose(code: number | undefined): void {
    switch (terminalCloseReason(code ?? 1006)) {
      case 'auth':
        unlocked.value = false
        phase.value = 'locked'
        error.value = 'The terminal grant expired. Unlock the terminal again.'
        return
      case 'origin':
        phase.value = 'error'
        error.value = 'The engine refused this terminal connection from this origin.'
        return
      case 'gone':
        phase.value = 'ended'
        error.value = 'That terminal session is gone.'
        return
      case 'takeover':
        phase.value = 'takeover'
        error.value = 'Another connection took over this terminal session.'
        return
      case 'slow':
        phase.value = 'error'
        error.value = 'The terminal connection was too slow and was closed.'
        return
      case 'retry':
        void scheduleRetry()
        return
    }
  }

  /**
   * An unexpected close is retried, but only after the status is read.
   *
   * A handshake refusal never reaches the browser as a close code — the
   * browser reports an abnormal closure instead — so the status is the only
   * way to learn that the grant is gone and the pane must be locked again.
   * The retry never creates a session: a reconnect resumes, it does not
   * launch.
   */
  async function scheduleRetry(): Promise<void> {
    reconnectAttempts += 1
    if (reconnectAttempts > MAX_AUTO_RECONNECT_ATTEMPTS) {
      phase.value = 'error'
      error.value = 'The terminal connection could not be re-established.'
      return
    }
    phase.value = 'reconnecting'
    const myScope = scopeEpoch
    const myConnection = connectionEpoch
    let statusKnown = false
    let statusUnlocked = false
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/status`)
      if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
      const parsed = parseStatus(raw)
      if (parsed) {
        statusKnown = true
        statusUnlocked = parsed.available && parsed.unlock_expires_in > 0
        status.value = parsed
      }
    } catch {
      // The engine is unreachable; that is a reason to retry, not to lock.
    }
    if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
    if (statusKnown && !statusUnlocked) {
      unlocked.value = false
      phase.value = 'locked'
      error.value = 'The terminal grant expired. Unlock the terminal again.'
      return
    }
    const delay = terminalReconnectDelayMs(reconnectAttempts)
    scheduleTimer(() => {
      if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
      if (!socketEligible()) return
      openSocket(false)
    }, delay)
  }

  function startPingTimer(): void {
    stopSocketTimers()
    pingTimer = setInterval(() => {
      const ws = socket
      if (!ws) return
      if (Date.now() - lastFrameAt >= WATCHDOG_MS) {
        // The socket says it is open but nothing has arrived for the whole
        // watchdog window: it is stalled, and a stalled socket is replaced.
        void forceReconnect()
        return
      }
      sendFrame(ws, { type: 'ping' })
    }, PING_INTERVAL_MS)
    if (disposed) stopSocketTimers()
  }

  async function forceReconnect(): Promise<void> {
    if (!socketEligible()) return
    phase.value = 'reconnecting'
    openSocket(false)
  }

  // ── Page lifecycle ─────────────────────────────────────────────────────

  function onVisibilityChange(): void {
    if (document.visibilityState !== 'visible') return
    resumeVisible()
  }

  function onPageShow(): void {
    resumeVisible()
  }

  /**
   * iOS suspends the page and its sockets in the background, and on resume
   * the socket can still report itself as open while nothing flows. The
   * connection is therefore replaced rather than trusted.
   */
  function resumeVisible(): void {
    if (!visible.value) return
    if (!socketEligible()) return
    openSocket(false)
  }

  function installListeners(): void {
    if (listenersInstalled) return
    if (typeof window === 'undefined' || typeof document === 'undefined') return
    listenersInstalled = true
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('pageshow', onPageShow)
  }

  function removeListeners(): void {
    if (!listenersInstalled) return
    if (typeof window === 'undefined' || typeof document === 'undefined') return
    listenersInstalled = false
    document.removeEventListener('visibilitychange', onVisibilityChange)
    window.removeEventListener('pageshow', onPageShow)
  }

  // ── Actions ────────────────────────────────────────────────────────────

  /**
   * Open the terminal for a scope.
   *
   * The status is read first: it is the only answer that says whether this
   * browser may use the terminal at all. Sessions are listed when the grant
   * is live, and an existing session is adopted — never created. Creating a
   * shell is an explicit action.
   */
  async function open(next: TerminalTarget): Promise<void> {
    const id = 'chatId' in next ? next.chatId : next.projectId
    if (!id || !id.trim()) {
      error.value = 'The terminal needs a chat or a project to run in.'
      phase.value = 'error'
      return
    }
    const scopeChanged = target.value === null || scopeKey(target.value) !== scopeKey(next)
    invalidateWorld()
    visible.value = true
    target.value = next
    if (scopeChanged) {
      scopeEpoch += 1
      sessions.value = []
      activeSession.value = null
      resetStream()
      truncated.value = false
    }
    error.value = ''
    warning.value = ''
    phase.value = 'loading'
    const myScope = scopeEpoch
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/status`)
      if (myScope !== scopeEpoch) return
      const parsed = parseStatus(raw)
      if (!parsed) {
        error.value = 'The engine did not report a terminal status this app understands.'
        phase.value = 'error'
        return
      }
      status.value = parsed
      if (!parsed.available) {
        unlocked.value = false
        phase.value = 'locked'
        error.value = unavailableMessage(parsed.reason)
        return
      }
      unlocked.value = parsed.unlock_expires_in > 0
      if (!unlocked.value) {
        phase.value = 'locked'
        return
      }
      phase.value = 'ready'
      await refreshSessions()
      if (myScope !== scopeEpoch) return
      maybeConnect()
    } catch {
      if (myScope !== scopeEpoch) return
      error.value = 'The engine could not be asked whether the terminal is available.'
      phase.value = 'error'
    }
  }

  function unavailableMessage(reason: string): string {
    if (reason === 'https_required') {
      return 'The terminal needs a secure connection. Open the app over HTTPS to use it.'
    }
    return 'The terminal is not available on this engine right now.'
  }

  /**
   * Hide the terminal.
   *
   * The socket is closed and the timers stop, but the session keeps running
   * on the engine: hiding is not ending, and ending is not hiding.
   *
   * The bytes the renderer never took are dropped, and the cursor goes back
   * to the acknowledged offset, because the engine replays from there: a
   * cursor left past the replay would trim the replay away and lose the
   * bytes between the two.
   */
  function hide(): void {
    visible.value = false
    invalidateWorld()
    dropUnacknowledged()
  }

  /**
   * Unlock the terminal.
   *
   * The password is handed to one request and is not kept anywhere: not in a
   * ref, not in a log, not in an error message. A successful unlock changes
   * the grant in the cookie, so the old socket is replaced on purpose — a
   * late close from the watchdog of the old connection must not decide what
   * the new one does.
   */
  async function unlock(password: string): Promise<boolean> {
    if (unlocking.value) return false
    const current = status.value
    if (!current) {
      error.value = 'Open the terminal before unlocking it.'
      return false
    }
    if (!current.available) {
      error.value = unavailableMessage(current.reason)
      return false
    }
    unlocking.value = true
    error.value = ''
    const myScope = scopeEpoch
    try {
      const raw = await api.post<unknown>(`${TERMINAL_API}/unlock`, { password })
      if (myScope !== scopeEpoch) return false
      const expires = parseUnlock(raw)
      if (expires === null) {
        error.value = 'The engine did not confirm the unlock.'
        phase.value = 'locked'
        return false
      }
      unlocked.value = true
      // A fresh grant is a fresh cookie: the old socket is dropped so its
      // late close cannot clobber the new connection.
      invalidateWorld()
      phase.value = 'ready'
      await refreshSessions()
      if (myScope !== scopeEpoch) return false
      maybeConnect()
      return true
    } catch {
      if (myScope !== scopeEpoch) return false
      unlocked.value = false
      phase.value = 'locked'
      error.value = 'The terminal did not unlock. Check the password and try again.'
      return false
    } finally {
      unlocking.value = false
    }
  }

  /**
   * Read the sessions of the current scope.
   *
   * A refresh that started before the user picked another session must not
   * pull the selection back. The selection that existed when the request
   * went out is what guards that: a list that arrives afterwards updates
   * the rows, never the choice.
   */
  async function refreshSessions(): Promise<void> {
    const scope = target.value
    if (!scope) return
    const myScope = scopeEpoch
    const selectedAtStart = activeSession.value?.session_id
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/sessions?${scopeQuery(scope)}`)
      if (myScope !== scopeEpoch) return
      const parsed = parseSessionList(raw)
      if (!parsed) {
        error.value = 'The engine did not report a session list this app understands.'
        return
      }
      sessions.value = parsed
      const current = activeSession.value
      if (current) {
        const fresh = parsed.find((session) => session.session_id === current.session_id)
        if (fresh) activeSession.value = fresh
        return
      }
      // A session was already selected when the request went out, and it is
      // gone from the list now: the list does not get to choose a different
      // one behind the user's back.
      if (selectedAtStart) return
      const candidate = parsed.find((session) => !session.exited)
      if (candidate) {
        activeSession.value = candidate
        selectionEpoch += 1
        resetStream()
      }
    } catch {
      if (myScope !== scopeEpoch) return
      error.value = 'The terminal sessions could not be listed.'
    }
  }

  /**
   * Create a session.
   *
   * This is the only path that starts a shell, and it is explicit: unlocking
   * the terminal does not create one, and a reconnect never does. A second
   * click while the first is in flight is refused rather than sent.
   */
  async function createSession(): Promise<boolean> {
    const scope = target.value
    if (!scope) return false
    if (creating.value) return false
    creating.value = true
    error.value = ''
    const myScope = scopeEpoch
    const myRenderer = rendererEpoch
    const mySelection = selectionEpoch
    try {
      const raw = await api.post<unknown>(`${TERMINAL_API}/sessions`, scopeBody(scope))
      if (myScope !== scopeEpoch || myRenderer !== rendererEpoch) return false
      const session = parseSessionEnvelope(raw)
      if (!session) {
        error.value = 'The engine did not report the new session.'
        return false
      }
      sessions.value = [
        ...sessions.value.filter((existing) => existing.session_id !== session.session_id),
        session,
      ]
      // The user may have picked another session while the request was open;
      // a new session does not yank the selection back.
      if (mySelection === selectionEpoch) {
        activeSession.value = session
        selectionEpoch += 1
        resetStream()
        phase.value = 'ready'
        maybeConnect()
      }
      return true
    } catch (caught) {
      if (myScope !== scopeEpoch) return false
      if (isRefusal(caught)) {
        error.value = 'The engine refused to start a terminal session.'
      } else {
        // The answer never arrived, so whether the session exists is unknown.
        // The request is not repeated: a second POST could start a second
        // shell. The list is refreshed instead.
        error.value =
          'The new terminal session could not be confirmed. Refresh the list to see whether it was created.'
      }
      return false
    } finally {
      creating.value = false
    }
  }

  /** A 4xx answer is a refusal: the engine says it did not create anything. */
  function isRefusal(caught: unknown): boolean {
    if (!caught || typeof caught !== 'object') return false
    const { status } = caught as { status?: unknown }
    return typeof status === 'number' && status >= 400 && status < 500
  }

  /** Choose one of the listed sessions. */
  function selectSession(sessionId: string): boolean {
    const session = sessions.value.find((entry) => entry.session_id === sessionId)
    if (!session) return false
    if (activeSession.value?.session_id === sessionId) return true
    invalidateWorld()
    activeSession.value = session
    selectionEpoch += 1
    // The offset belongs to the session: another one starts from zero.
    resetStream()
    phase.value = session.exited ? 'ended' : 'ready'
    maybeConnect()
    return true
  }

  /** End a session. This is the one action that stops the process. */
  async function endSession(): Promise<boolean> {
    const session = activeSession.value
    if (!session) return false
    if (ending.value) return false
    ending.value = true
    error.value = ''
    const myScope = scopeEpoch
    try {
      const raw = await api.del<unknown>(
        `${TERMINAL_API}/sessions/${encodeURIComponent(session.session_id)}`,
      )
      if (myScope !== scopeEpoch) return false
      if (!parseOk(raw)) {
        error.value = 'The engine did not confirm the end of the session.'
        return false
      }
      sessions.value = sessions.value.filter((entry) => entry.session_id !== session.session_id)
      if (activeSession.value?.session_id === session.session_id) {
        activeSession.value = null
        resetStream()
        phase.value = 'ended'
      }
      invalidateWorld()
      return true
    } catch {
      if (myScope !== scopeEpoch) return false
      error.value = 'The terminal session could not be ended.'
      return false
    } finally {
      ending.value = false
    }
  }

  /**
   * Attach the surface that draws the terminal.
   *
   * A renderer the store has not seen before starts at offset zero and takes
   * the retained replay; the same renderer attaching again resumes from the
   * offset it acknowledged. A renderer bound to another session is refused:
   * an offset means nothing in a different stream.
   */
  function attachRenderer(next: TerminalRenderer): void {
    if (renderer === next) return
    invalidateWorld()
    rendererEpoch += 1
    renderer = next
    const session = activeSession.value
    if (session && next.sessionId !== session.session_id) {
      error.value = 'The terminal view and the selected session do not match.'
      return
    }
    const known = rendererOffsets.get(next)
    consumed = known === undefined ? 0 : known
    screen = consumed
    clearQueue()
    error.value = ''
    maybeConnect()
  }

  /**
   * Detach a surface.
   *
   * The check is by identity: a detach for a renderer this store has already
   * replaced must not tear down the successor.
   */
  function detachRenderer(next: TerminalRenderer): void {
    if (renderer !== next) return
    invalidateWorld()
    rendererEpoch += 1
    renderer = null
    dropUnacknowledged()
  }

  /**
   * Reconnect.
   *
   * A session that another connection took over only comes back through an
   * explicit takeover, so the plain reconnect refuses while the pane is in
   * that state.
   */
  function reconnect(takeover = false): Promise<boolean> {
    if (phase.value === 'takeover' && !takeover) return Promise.resolve(false)
    if (!renderer || !activeSession.value) return Promise.resolve(false)
    reconnectAttempts = 0
    phase.value = 'reconnecting'
    openSocket(takeover)
    return Promise.resolve(true)
  }

  /** Send raw bytes to the shell. */
  function sendInput(bytes: Uint8Array): boolean {
    const ws = socket
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      error.value = 'The terminal is not connected.'
      return false
    }
    if (bytes.length > TERMINAL_INPUT_MAX_BYTES) {
      error.value = 'That input is too large for the terminal.'
      return false
    }
    if (bytes.length === 0) return true
    const frame = JSON.stringify({ type: 'input', data: encodeTerminalBytes(bytes) })
    if (ws.bufferedAmount + frame.length > SEND_BUFFER_MAX_BYTES) {
      error.value = 'The terminal is not keeping up. Try again in a moment.'
      return false
    }
    ws.send(frame)
    return true
  }

  /** Tell the shell about a new window size. */
  function resize(cols: number, rows: number): boolean {
    const ws = socket
    if (!ws || ws.readyState !== WebSocket.OPEN) return false
    if (!Number.isInteger(cols) || !Number.isInteger(rows)) return false
    // The engine's bounds are the client's bounds too: a frame outside them
    // is refused by the engine, and a zero-sized frame is not a window.
    const width = Math.min(Math.max(cols, TERMINAL_MIN_COLS), TERMINAL_MAX_COLS)
    const height = Math.min(Math.max(rows, TERMINAL_MIN_ROWS), TERMINAL_MAX_ROWS)
    ws.send(JSON.stringify({ type: 'resize', cols: width, rows: height }))
    return true
  }

  /** Tear everything down. The session on the engine keeps running. */
  function dispose(): void {
    if (disposed) return
    disposed = true
    scopeEpoch += 1
    invalidateWorld()
    clearQueue()
    for (const id of timers) clearTimeout(id)
    timers.clear()
    removeListeners()
    renderer = null
    visible.value = false
    phase.value = 'closed'
  }

  installListeners()

  onScopeDispose(() => {
    dispose()
  })

  return {
    // state
    visible,
    target,
    status,
    sessions,
    activeSession,
    phase,
    error,
    warning,
    truncated,
    unlocked,
    creating,
    ending,
    unlocking,
    // actions
    open,
    hide,
    unlock,
    refreshSessions,
    createSession,
    selectSession,
    endSession,
    attachRenderer,
    detachRenderer,
    reconnect,
    sendInput,
    resize,
    dispose,
  }
})
