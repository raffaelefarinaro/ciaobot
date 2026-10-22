import { defineStore } from 'pinia'
import { onScopeDispose, ref } from 'vue'
import { api } from '../lib/api'
import { requestTerminalPanel } from '../lib/terminalPanel'
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
// The grant is a cookie, and the session is its trust boundary. A terminal
// grant is a random bearer in an HttpOnly cookie, so the browser never holds
// it: there is no token in this file, no token in a ref, and every request
// rides the same-origin session the api helper already uses. There is no
// password: the pane asks the engine for a grant with an empty body, and the
// engine decides whether this signed-in, same-origin browser may hold the
// socket.
//
// Two offsets, and they are not the same number. `acked` is what the renderer
// has confirmed — the only offset a resume may use, because the contract says
// the client applies an offset only after the write callback completes.
// `rendered` is what the screen holds, and a replay is trimmed against it so
// the same bytes are never shown twice. `ready.seq` is neither: it says how
// many bytes exist.

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
 * what advances the acknowledged offset. `reset` clears the screen before a
 * truncated replay is written over it.
 */
export interface TerminalRenderer {
  /** A physical screen belongs to one session for its entire lifetime. */
  readonly sessionId: string
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
/** How long the engine may take to answer the attach with a ready frame. */
const READY_TIMEOUT_MS = 5000
/**
 * Automatic reconnect attempts before the client gives up and asks the user.
 *
 * Exported so a test can assert the exact number of attempts a bounded
 * retry chain makes, rather than only that it stopped.
 */
export const MAX_AUTO_RECONNECT_ATTEMPTS = 8
/** Sessions the engine keeps for one scope. */
const MAX_SESSIONS = 4

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
  // A session without a folder is not a session the client can show: the
  // pane names the folder, and an empty one would read as "no folder".
  if (typeof folder !== 'string' || !folder.trim()) return null
  if (folder.length > TERMINAL_MAX_FOLDER_CHARS) return null
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
  // The engine keeps four sessions per scope. A longer list is not a list
  // this client can act on, and rendering it would mean the client believes
  // more sessions than the engine can hold.
  if (sessions.length > MAX_SESSIONS) return null
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

  // ── Internals ──────────────────────────────────────────────────────────

  let disposed = false
  /** Bumped when the scope changes, so a reply for the previous scope cannot land. */
  let scopeEpoch = 0
  /**
   * Bumped when the pane is opened again, so a reply for an earlier open of
   * the *same* scope cannot land either: the scope did not change, but the
   * pane was reopened, and the older answer is about a pane that is gone.
   */
  let openEpoch = 0
  /**
   * Bumped when the session list is read again, so a reply for an earlier
   * read cannot land on a newer one.
   */
  let refreshEpoch = 0
  /** Bumped when the connection is replaced, so a pending retry cannot reopen over it. */
  let connectionEpoch = 0
  /** The renderer currently drawing the terminal, if any. */
  let renderer: TerminalRenderer | null = null
  /** The socket currently owned by the store, if any. */
  let socket: WebSocket | null = null
  /** True once the current connection completed the terminal handshake. */
  let terminalReady = false
  /** Automatic reconnect attempts since the last completed handshake. */
  let reconnectAttempts = 0
  /** When the last valid frame arrived, in milliseconds. */
  let lastFrameAt = 0

  /**
   * What one surface holds, and everything the store owes it.
   *
   * A surface is a physical screen bound to one session for its lifetime. It
   * has its own bytes and place in that stream; a detached surface keeps its
   * own numbers, so selecting another session never costs it what it showed.
   */
  interface Surface {
    /** The renderer this record belongs to. */
    view: TerminalRenderer
    /** The session this physical screen shows for its lifetime. */
    readonly sessionId: string
    /** Bytes the renderer has confirmed — the only offset a resume may use. */
    acked: number
    /**
     * The end of the bytes the surface's screen holds.
     *
     * This is what the screen really shows, not what the store has accepted:
     * a byte that was queued and then dropped was never on the screen, so it
     * must not be trimmed away when the engine replays it.
     */
    shown: number
    /** The bytes waiting to be written to this surface. */
    queue: QueuedChunk[]
    /** Bytes the queue holds. */
    queued: number
    /** True while a write to this surface is in flight. */
    writing: boolean
    /** The end of the bytes the in-flight write carries, if there is one. */
    inflightEnd: number | null
    /** A clear this surface owes, and the offset it clears back to. */
    pendingReset: number | null
    /** True while this surface is the one on show. */
    attached: boolean
  }

  interface QueuedChunk {
    bytes: Uint8Array
    /** The offset the stream reaches once these bytes are on the screen. */
    nextOffset: number
  }

  /** One physical renderer has one immutable session record. */
  const surfaces = new WeakMap<TerminalRenderer, Surface>()

  function surfaceOf(view: TerminalRenderer): Surface {
    const known = surfaces.get(view)
    if (known) return known
    const fresh: Surface = {
      view,
      sessionId: view.sessionId,
      acked: 0,
      shown: 0,
      queue: [],
      queued: 0,
      writing: false,
      inflightEnd: null,
      pendingReset: null,
      attached: false,
    }
    surfaces.set(view, fresh)
    return fresh
  }

  /** The surface on show, if there is one. */
  function currentSurface(): Surface | null {
    return renderer ? surfaces.get(renderer) ?? null : null
  }

  const timers = new Set<ReturnType<typeof setTimeout>>()
  let pingTimer: ReturnType<typeof setInterval> | null = null
  let readyTimer: ReturnType<typeof setTimeout> | null = null
  let listenersInstalled = false

  function scheduleTimer(fn: () => void, ms: number): ReturnType<typeof setTimeout> {
    const id = setTimeout(() => {
      timers.delete(id)
      if (!disposed) fn()
    }, ms)
    if (!disposed) timers.add(id)
    return id
  }

  function cancelTimer(id: ReturnType<typeof setTimeout> | null): void {
    if (id === null || id === undefined) return
    clearTimeout(id)
    timers.delete(id)
  }

  /** Close the socket without letting its close event decide anything. */
  function closeSocket(): void {
    const ws = socket
    socket = null
    connectionEpoch += 1
    terminalReady = false
    cancelTimer(readyTimer)
    readyTimer = null
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
    cancelTimer(readyTimer)
    readyTimer = null
  }

  /** Drop the connection and its timers. */
  function invalidateWorld(): void {
    closeSocket()
    stopSocketTimers()
  }

  function fail(message: string): void {
    error.value = message
    phase.value = 'error'
    invalidateWorld()
  }

  // ── Renderer backpressure ──────────────────────────────────────────────

  /**
   * Drop the bytes a surface is waiting for.
   *
   * They never reached the screen, so the surface's shown offset goes back to
   * what it really shows — the acknowledged bytes, plus the write that is
   * already in flight and cannot be recalled. The engine replays from the
   * acknowledged offset, so the discarded bytes come back rather than being
   * trimmed away as if they had been shown.
   */
  function dropQueued(surface: Surface): void {
    const droppedBytes = surface.queue.reduce((total, chunk) => total + chunk.bytes.length, 0)
    surface.queue = []
    // `queued` includes the write already in flight. Only the queued tail is
    // discarded here; the write cannot be recalled and stays counted until
    // its promise settles.
    surface.queued -= droppedBytes
    surface.shown = Math.max(surface.acked, surface.inflightEnd ?? surface.acked)
  }

  /**
   * Clear a surface before the first write that follows a truncated replay.
   *
   * The clear waits for the write in flight: a write that is still on its way
   * would otherwise land on the cleared screen and put the bytes back. The
   * wait belongs to the surface, so a surface that is detached while it waits
   * is still cleared — once, and after its own write has finished.
   */
  function applyReset(surface: Surface, at: number): void {
    surface.pendingReset = null
    surface.view.reset()
    surface.acked = at
    surface.shown = at
    if (surface.attached) {
      truncated.value = true
      warning.value =
        'The retained output was truncated, so the terminal may not show the full screen state.'
    }
  }

  /**
   * One drain loop per surface, not one task per frame.
   *
   * The renderer writes asynchronously, so the bytes it has not taken yet
   * wait in the surface's own queue. The queue is bounded: past a megabyte
   * the client is slower than the shell, and the socket is closed rather
   * than allowed to grow.
   */
  async function drain(surface: Surface): Promise<void> {
    if (surface.writing) return
    surface.writing = true
    try {
      while (surface.queue.length > 0) {
        if (disposed) {
          dropQueued(surface)
          return
        }
        const chunk = surface.queue.shift() as QueuedChunk
        // The bytes stay counted while they are on their way to the screen:
        // the write in flight is part of what the client is holding.
        surface.inflightEnd = chunk.nextOffset
        if (surface.pendingReset !== null) applyReset(surface, surface.pendingReset)
        try {
          await surface.view.write(chunk.bytes)
        } catch {
          // A failed write belongs to the surface that asked for it. A
          // surface that is not on show, or a pane that is hidden, is not
          // the store's to answer for: the successor keeps its own state
          // and the user is not looking at a screen that is not there.
          surface.inflightEnd = null
          surface.queued -= chunk.bytes.length
          if (surface.attached && visible.value) {
            fail('The terminal view could not be written to.')
          }
          return
        }
        surface.inflightEnd = null
        surface.queued -= chunk.bytes.length
        // The renderer took the bytes, so the surface's screen holds them.
        // The acknowledgement belongs to the surface, not to the socket.
        if (chunk.nextOffset > surface.acked) {
          surface.acked = chunk.nextOffset
        }
        if (chunk.nextOffset > surface.shown) {
          surface.shown = chunk.nextOffset
        }
      }
    } finally {
      surface.inflightEnd = null
      // A reset is an operation on the physical screen, not merely this
      // session's cursor. It must finish before this drain releases the
      // renderer to another session; otherwise the old reset can erase a
      // successor write that has already begun.
      if (surface.pendingReset !== null) {
        applyReset(surface, surface.pendingReset)
      }
      surface.writing = false
      if (surface.queue.length > 0 && !disposed) void drain(surface)
    }
  }

  /**
   * Queue the bytes for a surface.
   *
   * The bytes are on their way to the screen, so the surface's shown offset
   * moves now. The *acknowledged* offset moves later, and only that one goes
   * into an `attach`.
   */
  function enqueueOutput(surface: Surface, bytes: Uint8Array, nextOffset: number): void {
    if (bytes.length === 0) return
    if (surface.queued + bytes.length > PENDING_OUTPUT_MAX_BYTES) {
      // The shell is outrunning the screen. The stream is broken, so it is
      // not retried: a new connection would replay the same output into the
      // same slow screen.
      fail('The shell is producing output faster than the terminal can show it.')
      return
    }
    surface.queued += bytes.length
    if (nextOffset > surface.shown) surface.shown = nextOffset
    surface.queue.push({ bytes, nextOffset })
    void drain(surface)
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

  /**
   * A connection may be opened only while the surface, the scope, the grant
   * and the visibility all agree.
   */
  function canConnect(): boolean {
    if (disposed) return false
    if (!visible.value || !unlocked.value) return false
    const surface = currentSurface()
    const session = activeSession.value
    if (!surface || !session) return false
    return surface.sessionId === session.session_id
  }

  /**
   * An automatic recovery may open a connection only where the user has not
   * something to decide.
   *
   * A session another connection took over comes back through an explicit
   * takeover, a session that is gone does not come back at all, a pane with
   * no grant has to ask for one first, and a failed stream needs the user to
   * look at it.
   */
  function canRecover(): boolean {
    if (!canConnect()) return false
    return phase.value !== 'takeover'
      && phase.value !== 'ended'
      && phase.value !== 'locked'
      && phase.value !== 'error'
  }

  function maybeConnect(): void {
    if (!canConnect()) return
    if (socket && socket.readyState <= WebSocket.OPEN) return
    openSocket(false)
  }

  function openSocket(takeover: boolean): void {
    const session = activeSession.value
    const surface = currentSurface()
    if (!session || !surface) return
    closeSocket()
    // The resume point belongs to the surface that will receive the replay.
    const ws = new WebSocket(terminalSocketUrl(session.session_id))
    socket = ws
    lastFrameAt = Date.now()
    phase.value = 'connecting'

    ws.onopen = () => {
      if (socket !== ws) return
      lastFrameAt = Date.now()
      // The resume point is the offset the renderer acknowledged — never
      // `ready.seq`, which is only a snapshot of what exists.
      sendFrame(ws, { type: 'attach', offset: surface.acked, takeover })
      // The transport is open, but the terminal is not: the engine still has
      // to say the session is ready. A connection that never gets there is
      // closed and retried, and it costs the retry budget.
      readyTimer = scheduleTimer(() => {
        if (socket !== ws) return
        error.value = 'The terminal did not complete its handshake.'
        closeSocket()
        handleClose(1006)
      }, READY_TIMEOUT_MS)
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
    const surface = currentSurface()
    if (!surface) return
    switch (frame.type) {
      case 'ready': {
        if (frame.session_id !== activeSession.value?.session_id) {
          fail('The terminal connected to a different session.')
          return
        }
        // The handshake is complete: the terminal is live, and the retry
        // budget starts again.
        terminalReady = true
        cancelTimer(readyTimer)
        readyTimer = null
        reconnectAttempts = 0
        phase.value = 'live'
        // Only a live terminal is worth keeping alive with a ping: a socket
        // that never got here is closed by the handshake timeout instead.
        startPingTimer()
        // The engine replays from the offset the client acknowledged, so the
        // gap is measured against that offset. A retained buffer that starts
        // after it means the bytes in between are gone.
        if (frame.replay_start > surface.acked) {
          if (!frame.truncated) {
            // The engine no longer holds the bytes between the client's cursor
            // and its buffer, and does not admit to having lost them: the
            // stream cannot be continued without inventing bytes.
            fail('The terminal skipped output that never reached this browser.')
            return
          }
          // The retained stream starts after the cursor and the engine says
          // it lost the rest. The screen is cleared before the replay is
          // written over it, and the clear waits for the write in flight so
          // stale bytes cannot land on it. The bytes queued for the dropped
          // stream go with it: the engine is about to send them again.
          dropQueued(surface)
          surface.acked = frame.replay_start
          surface.shown = frame.replay_start
          surface.pendingReset = frame.replay_start
          truncated.value = true
          warning.value =
            'The retained output was truncated, so the terminal may not show the full screen state.'
        }
        if (frame.truncated) truncated.value = true
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
        const slice = terminalOutputSlice(frame, surface.shown)
        if (!slice) {
          fail('The terminal sent output this browser never received.')
          return
        }
        // A duplicate carries no new bytes. It is discarded, and it
        // acknowledges nothing: only the renderer may do that, by resolving
        // the write.
        if (slice.bytes.length === 0) break
        enqueueOutput(surface, slice.bytes, slice.nextOffset)
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
        invalidateWorld()
        break
      }
      case 'pong':
        break
      case 'error': {
        error.value = frame.reason
        phase.value = 'error'
        invalidateWorld()
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
        // The engine ended the stream because the grant is gone: it
        // expired, or it was revoked with the session behind it. Nothing
        // is asked of the user — the session is the trust boundary, so
        // the pane asks for a new grant and resumes on its own.
        unlocked.value = false
        void scheduleRetry()
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
   * way to learn that the grant is gone. A missing grant is not a dead
   * end: the pane asks the engine for a new one, because the signed-in
   * session is the trust boundary and there is no password to re-enter.
   * A grant the engine will not give is not a dead end either: the next
   * attempt asks again, on the same backoff, until the attempt budget
   * runs out. The retry never creates a session: a reconnect resumes, it
   * does not launch.
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
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/status`)
      if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
      const parsed = parseStatus(raw)
      if (parsed) {
        status.value = parsed
        if (!parsed.available) {
          unlocked.value = false
          phase.value = 'locked'
          error.value = unavailableMessage(parsed.reason)
          return
        }
        unlocked.value = parsed.unlock_expires_in > 0
      }
    } catch {
      // The engine is unreachable; that is a reason to retry, not to lock.
    }
    if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
    if (!unlocked.value) {
      // The grant is gone, or the status could not be read and the pane
      // knows it has none: a signed-in, same-origin browser gets a new
      // one on its own.
      const granted = await acquireGrant()
      if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
      if (!granted) {
        // The engine would not give us a grant this time. That is not the
        // end of the road: there is no form for the user to fill in any
        // more, so the next attempt asks again, on the same backoff, until
        // the attempt budget runs out.
        scheduleTimer(() => {
          if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
          void scheduleRetry()
        }, terminalReconnectDelayMs(reconnectAttempts))
        return
      }
    }
    error.value = ''
    const delay = terminalReconnectDelayMs(reconnectAttempts)
    scheduleTimer(() => {
      if (myScope !== scopeEpoch || myConnection !== connectionEpoch) return
      if (!canRecover()) return
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
        if (!canRecover()) return
        phase.value = 'reconnecting'
        openSocket(false)
        return
      }
      sendFrame(ws, { type: 'ping' })
    }, PING_INTERVAL_MS)
    if (disposed) stopSocketTimers()
  }

  // ── Page lifecycle ─────────────────────────────────────────────────────

  /**
   * iOS suspends the page and its sockets in the background, and on resume
   * the socket can still report itself as open while nothing flows. The
   * connection is therefore replaced rather than trusted.
   */
  function resumeVisible(): void {
    if (!canRecover()) return
    openSocket(false)
  }

  function onVisibilityChange(): void {
    if (document.visibilityState !== 'visible') return
    resumeVisible()
  }

  function onPageShow(): void {
    resumeVisible()
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
   * browser may use the terminal at all. The grant comes next, and the pane
   * asks for it itself — there is no password to re-enter, because the
   * signed-in session is the terminal's trust boundary. A grant the engine
   * will not give is not the end of the road either: the pane asks again,
   * on the same bounded backoff a lost connection uses. Sessions are listed
   * when the grant is live, and an existing session is adopted — never
   * created. Creating a shell is an explicit action.
   */
  async function open(next: TerminalTarget): Promise<void> {
    const id = 'chatId' in next ? next.chatId : next.projectId
    if (!id || !id.trim()) {
      error.value = 'The terminal needs a chat or a project to run in.'
      phase.value = 'error'
      return
    }
    // The pane is a chunk of its own, mounted by the shell the first time a
    // terminal is asked for. Asking for it here keeps every way into the
    // terminal — the chat's button, the project's button, a future deep link
    // — on the same path.
    requestTerminalPanel()
    const scopeChanged = target.value === null || scopeKey(target.value) !== scopeKey(next)
    invalidateWorld()
    visible.value = true
    installListeners()
    target.value = next
    if (scopeChanged) {
      scopeEpoch += 1
      sessions.value = []
      activeSession.value = null
      truncated.value = false
    }
    // The retry budget belongs to the pane, not to the session: a pane
    // that opens gets a whole budget of its own, and the chain the
    // previous one left behind is stale (its connection epoch is gone).
    reconnectAttempts = 0
    error.value = ''
    warning.value = ''
    phase.value = 'loading'
    const myScope = scopeEpoch
    const myOpen = ++openEpoch
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/status`)
      if (myScope !== scopeEpoch || myOpen !== openEpoch) return
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
      if (parsed.unlock_expires_in > 0) {
        // The grant in the cookie is still live: it is not re-minted, so
        // two tabs of one browser do not revoke each other's grant.
        unlocked.value = true
      } else {
        // No grant yet. A signed-in, same-origin browser gets one on its
        // own, so the pane opens without asking for anything.
        const granted = await acquireGrant()
        if (myScope !== scopeEpoch || myOpen !== openEpoch) return
        if (!granted) {
          // The engine would not give us a grant. There is no form for the
          // user to fill in any more, so the next attempt asks again, on
          // the same backoff and the same budget a lost connection uses.
          // The counter was reset above, so this first wait is the delay
          // of attempt 1 — the attempt the timer is about to make.
          scheduleTimer(() => {
            if (myScope !== scopeEpoch || myOpen !== openEpoch) return
            if (!visible.value) return
            void scheduleOpenRetry()
          }, terminalReconnectDelayMs(reconnectAttempts + 1))
          return
        }
      }
      phase.value = 'ready'
      await refreshSessions()
      if (myScope !== scopeEpoch || myOpen !== openEpoch) return
      maybeConnect()
    } catch {
      if (myScope !== scopeEpoch || myOpen !== openEpoch) return
      error.value = 'The engine could not be asked whether the terminal is available.'
      phase.value = 'error'
    }
  }

  /**
   * The open path's own continuation.
   *
   * `open` asked for a grant and the engine would not give one. This asks
   * again — on the same backoff and the same attempt budget a lost
   * connection uses — and resumes where `open` stopped when the grant
   * arrives: the pane becomes ready, the sessions are listed, and the
   * socket opens once a surface attaches.
   *
   * It is deliberately not `scheduleRetry`: that one resumes a
   * *connection*, and it needs a session and a screen to attach to, which a
   * pane that has just opened does not have — `open` stopped before the
   * session list.
   */
  async function scheduleOpenRetry(): Promise<void> {
    reconnectAttempts += 1
    if (reconnectAttempts > MAX_AUTO_RECONNECT_ATTEMPTS) {
      phase.value = 'error'
      error.value = 'The terminal connection could not be re-established.'
      return
    }
    const myScope = scopeEpoch
    const myOpen = openEpoch
    const granted = await acquireGrant()
    if (myScope !== scopeEpoch || myOpen !== openEpoch) return
    // A hidden pane asks for nothing and says nothing: the chain stops
    // here rather than running behind the user's back.
    if (!visible.value) return
    if (!granted) {
      // The engine would not give us a grant this time. The next attempt
      // asks again, until the attempt budget runs out.
      scheduleTimer(() => {
        if (myScope !== scopeEpoch || myOpen !== openEpoch) return
        if (!visible.value) return
        void scheduleOpenRetry()
      }, terminalReconnectDelayMs(reconnectAttempts))
      return
    }
    // The grant is here: the pane resumes where `open` stopped.
    phase.value = 'ready'
    error.value = ''
    await refreshSessions()
    if (myScope !== scopeEpoch || myOpen !== openEpoch) return
    if (!visible.value) return
    maybeConnect()
  }

  /**
   * Ask the engine for this browser's terminal grant.
   *
   * There is no password and nothing to re-enter: the signed-in session is
   * the terminal's trust boundary, so the engine decides whether this
   * browser may hold the socket. The body is empty — the route takes none —
   * and the grant itself never reaches this code: it arrives as a cookie.
   *
   * The answer only counts for the open that asked: an older open of the
   * same scope must not write over the state of the newer one, so the
   * open epoch is checked before anything is written.
   */
  async function acquireGrant(): Promise<boolean> {
    const myScope = scopeEpoch
    const myOpen = openEpoch
    try {
      const raw = await api.post<unknown>(`${TERMINAL_API}/unlock`)
      if (myScope !== scopeEpoch || myOpen !== openEpoch) return false
      const expires = parseUnlock(raw)
      if (expires === null) {
        unlocked.value = false
        phase.value = 'locked'
        error.value = 'The engine did not confirm the terminal grant.'
        return false
      }
      unlocked.value = true
      return true
    } catch {
      if (myScope !== scopeEpoch || myOpen !== openEpoch) return false
      unlocked.value = false
      phase.value = 'locked'
      error.value = 'The terminal grant could not be obtained.'
      return false
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
   * The bytes the surface is waiting for are dropped, and the surface's
   * shown offset goes back to what it really shows. The engine replays from
   * the acknowledged offset, so the dropped bytes come back rather than
   * being trimmed away as if the screen had already shown them.
   */
  function hide(): void {
    visible.value = false
    invalidateWorld()
    removeListeners()
    const surface = currentSurface()
    if (surface) dropQueued(surface)
  }

  /**
   * Read the sessions of the current scope.
   *
   * A refresh that started before the user picked another session must not
   * pull the selection back. The selection that existed when the request
   * went out is what guards that: a list that arrives afterwards updates
   * the rows, never the choice.
   *
   * A refresh that started before a *later* refresh is stale whatever the
   * scope, so the newest request is the one that answers.
   */
  async function refreshSessions(): Promise<void> {
    const scope = target.value
    if (!scope) return
    const myScope = scopeEpoch
    const myRefresh = ++refreshEpoch
    const selectedAtStart = activeSession.value?.session_id
    try {
      const raw = await api.get<unknown>(`${TERMINAL_API}/sessions?${scopeQuery(scope)}`)
      if (myScope !== scopeEpoch || myRefresh !== refreshEpoch) return
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
      }
    } catch {
      // A failure that belongs to a read that has been superseded is not
      // the newer one's error to show.
      if (myScope !== scopeEpoch || myRefresh !== refreshEpoch) return
      error.value = 'The terminal sessions could not be listed.'
    }
  }

  /**
   * Create a session.
   *
   * This is the only path that starts a shell, and it is explicit: opening
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
    const selectedAtStart = activeSession.value?.session_id
    try {
      const raw = await api.post<unknown>(`${TERMINAL_API}/sessions`, scopeBody(scope))
      if (myScope !== scopeEpoch) return false
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
      if (activeSession.value?.session_id !== selectedAtStart) return true
      // The new session takes over from the old one, and the old one keeps
      // its connection to itself: the socket belongs to a session, so it
      // goes with the session it was opened for.
      invalidateWorld()
      activeSession.value = session
      // The surface keeps its bytes: it is the same screen, and the session
      // it was showing may well be selected again.
      phase.value = 'ready'
      maybeConnect()
      return true
    } catch (caught) {
      // A failure that belongs to a session that is no longer selected is
      // not the successor's error to show.
      if (myScope !== scopeEpoch) return false
      if (activeSession.value?.session_id !== selectedAtStart) return false
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

  /**
   * Choose one of the listed sessions.
   *
   * The socket belongs to a session, so the old one goes with the old
   * selection. The *surface* does not: a surface showing another session
   * keeps what it showed, and comes back to it when its session is selected
   * again. Selecting another session is not permission to erase what the
   * first one showed.
   */
  function selectSession(sessionId: string): boolean {
    const session = sessions.value.find((entry) => entry.session_id === sessionId)
    if (!session) return false
    if (activeSession.value?.session_id === sessionId) return true
    invalidateWorld()
    activeSession.value = session
    phase.value = session.exited ? 'ended' : 'ready'
    maybeConnect()
    return true
  }

  /**
   * End a session. This is the one action that stops the process.
   *
   * The reply only touches the session it was asked about. A session that was
   * selected while the request was in flight keeps its own connection: the
   * list loses the ended row, and nothing else moves.
   */
  async function endSession(): Promise<boolean> {
    const session = activeSession.value
    if (!session) return false
    if (ending.value) return false
    ending.value = true
    error.value = ''
    const myScope = scopeEpoch
    const endedId = session.session_id
    try {
      const raw = await api.del<unknown>(
        `${TERMINAL_API}/sessions/${encodeURIComponent(endedId)}`,
      )
      if (myScope !== scopeEpoch) return false
      if (!parseOk(raw)) {
        if (activeSession.value?.session_id !== endedId) return false
        error.value = 'The engine did not confirm the end of the session.'
        return false
      }
      sessions.value = sessions.value.filter((entry) => entry.session_id !== endedId)
      // Only the session that was asked about is torn down. A successor that
      // was selected while the request was open keeps its own socket.
      if (activeSession.value?.session_id !== endedId) return true
      invalidateWorld()
      activeSession.value = null
      phase.value = 'ended'
      return true
    } catch {
      // A failure that belongs to a session that is no longer selected is
      // not the successor's error to show.
      if (myScope !== scopeEpoch) return false
      if (activeSession.value?.session_id !== endedId) return false
      error.value = 'The terminal session could not be ended.'
      return false
    } finally {
      ending.value = false
    }
  }

  /**
   * Attach the surface that draws the terminal.
   *
   * A surface the store has not seen before starts at offset zero and takes
   * the retained replay; the same surface attaching again resumes from the
   * offset it acknowledged. A surface is permanently bound to its first
   * session: an integration switching sessions must create a new renderer,
   * because a physical screen cannot retain two session histories.
   */
  function attachRenderer(next: TerminalRenderer): void {
    const session = activeSession.value
    if (session && next.sessionId !== session.session_id) {
      error.value = 'The terminal view and the selected session do not match.'
      return
    }
    const known = surfaces.get(next)
    if (known && known.sessionId !== next.sessionId) {
      error.value = 'A terminal view cannot be reused for another session.'
      return
    }
    if (renderer === next) return
    invalidateWorld()
    const previous = currentSurface()
    if (previous) previous.attached = false
    renderer = next
    const surface = surfaceOf(next)
    surface.attached = true
    error.value = ''
    maybeConnect()
  }

  /**
   * Detach a surface.
   *
   * The check is by identity: a detach for a surface this store has already
   * replaced must not tear down the successor.
   *
   * The surface keeps its own bytes and its own pending clear: a clear it
   * owes is owed to that screen, and it waits for that screen's write in
   * flight however long that takes. The store does not clear it on the
   * spot, because the write in flight would land on the cleared screen.
   */
  function detachRenderer(next: TerminalRenderer): void {
    if (renderer !== next) return
    invalidateWorld()
    const surface = currentSurface()
    if (surface) surface.attached = false
    renderer = null
  }

  /**
   * Reconnect.
   *
   * A session that another connection took over only comes back through an
   * explicit takeover, and a hidden or locked pane has no connection to
   * restore.
   */
  function reconnect(takeover = false): Promise<boolean> {
    if (phase.value === 'takeover' && !takeover) return Promise.resolve(false)
    if (!canConnect()) return Promise.resolve(false)
    reconnectAttempts = 0
    phase.value = 'reconnecting'
    openSocket(takeover)
    return Promise.resolve(true)
  }

  /** Send raw bytes to the shell. */
  function sendInput(bytes: Uint8Array): boolean {
    const ws = socket
    if (!ws || ws.readyState !== WebSocket.OPEN || !terminalReady) {
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
    if (!ws || ws.readyState !== WebSocket.OPEN || !terminalReady) return false
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
    // The current surface loses its queued tail. Other surfaces with a drain
    // stop themselves after their uncancelable write settles, without keeping
    // every historical renderer alive in a global registry.
    const surface = currentSurface()
    if (surface) {
      dropQueued(surface)
      surface.attached = false
    }
    for (const id of timers) clearTimeout(id)
    timers.clear()
    removeListeners()
    renderer = null
    visible.value = false
    phase.value = 'closed'
  }

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
    // actions
    open,
    hide,
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
