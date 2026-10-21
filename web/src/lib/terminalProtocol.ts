// The embedded terminal's wire protocol: types, strict validation and
// replay-offset arithmetic.
//
// Pure by design — no Vue, no socket, no storage, no rendering; `lib/chatWs.ts`
// has the same shape. A terminal frame carries raw bytes, so every rule here
// is about *bytes*, never about text: a byte sequence that is not valid UTF-8
// is normal for a shell to emit, and a decoder that "fixes" it corrupts the
// stream. The limits are the engine's, not the browser's, and constants
// rather than settings: a terminal that can ask for more than the engine
// grants is a terminal that can wedge the engine.

/** Replay buffer the engine keeps per session, in bytes. */
export const TERMINAL_REPLAY_MAX_BYTES = 256 * 1024
/** Bytes in a single input or output frame; bounds both directions. */
export const TERMINAL_INPUT_MAX_BYTES = 65536
/** Largest WebSocket envelope the terminal socket accepts, in bytes. */
export const TERMINAL_ENVELOPE_MAX_BYTES = 1024 * 1024
/** Terminal window bounds, in character cells. */
export const TERMINAL_MIN_COLS = 20
export const TERMINAL_MAX_COLS = 500
export const TERMINAL_MIN_ROWS = 5
export const TERMINAL_MAX_ROWS = 200
/** Longest error reason the client will show, in characters. */
export const TERMINAL_MAX_REASON_CHARS = 128
/** Longest session id the client accepts, in characters. */
export const TERMINAL_MAX_SESSION_ID_CHARS = 128
/** Longest folder path the client accepts, in characters. */
export const TERMINAL_MAX_FOLDER_CHARS = 4096

/** A terminal session as the HTTP endpoints describe it. */
export interface TerminalSessionInfo {
  session_id: string
  folder: string
  cols: number
  rows: number
  /** Absolute byte count of the session's output stream so far. */
  seq: number
  attached: boolean
  exited: boolean
  exit_code: number | null
}

/**
 * Frames the engine sends. `ready.seq` is a snapshot of the stream, not an
 * instruction: it says how many bytes exist, never how many the client has
 * consumed. `output`'s `[start, end)` is a byte interval and `data` is the
 * raw bytes in strict standard base64.
 */
export type TerminalServerFrame =
  | {
      type: 'ready'
      session_id: string
      folder: string
      cols: number
      rows: number
      seq: number
      /** First byte offset still held in the replay buffer. */
      replay_start: number
      truncated: boolean
    }
  | { type: 'output'; start: number; end: number; data: string }
  | { type: 'replay_end'; seq: number; truncated: boolean }
  | { type: 'exit'; code: number | null }
  | { type: 'pong' }
  | { type: 'error'; reason: string }

/** Frames the client sends. `input.data` is strict standard base64 of the raw
 *  bytes, so a keystroke and a pasted blob travel the same way. */
export type TerminalClientFrame =
  | { type: 'attach'; offset: number; takeover: boolean }
  | { type: 'input'; data: string }
  | { type: 'resize'; cols: number; rows: number }
  | { type: 'ping' }

/** Longest base64 string that can hold a legal frame. */
const MAX_ENCODED_CHARS = Math.ceil(TERMINAL_INPUT_MAX_BYTES / 3) * 4

/**
 * Encode raw bytes as strict standard base64. The binary string is built in
 * chunks rather than by spreading the buffer into a call —
 * `String.fromCharCode(...bytes)` throws on a 64 KiB buffer.
 */
export function encodeTerminalBytes(bytes: Uint8Array): string {
  let binary = ''
  for (let start = 0; start < bytes.length; start += 8192) {
    const chunk = bytes.subarray(start, start + 8192)
    binary += String.fromCharCode.apply(null, chunk as unknown as number[])
  }
  return btoa(binary)
}

/**
 * Decode strict standard base64 into raw bytes, or `null` when the string is
 * not base64 this client is willing to accept. "Strict" means the standard
 * alphabet only (not base64url), a length that is a multiple of four, and
 * trailing bits a partial group cannot carry set to zero — so every byte
 * sequence has exactly one encoding. `atob` accepts more, so the decoded
 * bytes are re-encoded and compared: a string whose canonical form differs
 * from itself is not one to act on.
 */
export function decodeTerminalBytes(data: string): Uint8Array | null {
  if (typeof data !== 'string') return null
  if (data.length % 4 !== 0) return null
  if (data.length === 0) return new Uint8Array(0)
  if (data.length > MAX_ENCODED_CHARS) return null

  let binary: string
  try {
    binary = atob(data)
  } catch {
    return null
  }
  if (btoa(binary) !== data) return null

  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i)
  if (bytes.length > TERMINAL_INPUT_MAX_BYTES) return null
  return bytes
}

/**
 * The keys each frame type carries.
 *
 * A plain object, so the lookup has to be an own-property check: a frame
 * type that happens to be an inherited `Object.prototype` name —
 * "constructor", "hasOwnProperty", "__proto__" — is not a key of this table.
 * The type comes off the wire, so a name that merely *looks* like a built-in
 * must resolve to nothing, not to a function sitting on the prototype.
 */
const FRAME_KEYS: Record<string, readonly string[]> = {
  ready: ['type', 'session_id', 'folder', 'cols', 'rows', 'seq', 'replay_start', 'truncated'],
  output: ['type', 'start', 'end', 'data'],
  replay_end: ['type', 'seq', 'truncated'],
  exit: ['type', 'code'],
  pong: ['type'],
  error: ['type', 'reason'],
}

/** True when the frame carries exactly these keys — no more, no fewer. */
function hasExactKeys(frame: Record<string, unknown>, keys: readonly string[]): boolean {
  const own = Object.keys(frame)
  if (own.length !== keys.length) return false
  return keys.every((key) => Object.prototype.hasOwnProperty.call(frame, key))
}

/** A byte offset or stream position: a non-negative safe integer, never a boolean. */
function isOffset(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

/** A cell count inside the engine's terminal bounds, never a boolean. */
function isCellCount(value: unknown, min: number, max: number): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max
}

/** A non-empty string that is not only whitespace. */
function isFilledString(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0
}

/**
 * True when the UTF-8 encoding of `value` is longer than `cap` bytes. The
 * UTF-16 length is a lower bound on the UTF-8 length, so a string already too
 * long in characters is refused without encoding it; what survives is encoded
 * and measured, because a multi-byte character costs three UTF-8 bytes and
 * one UTF-16 code unit.
 */
function exceedsUtf8Cap(value: string, cap: number): boolean {
  if (value.length > cap) return true
  return new TextEncoder().encode(value).length > cap
}

/**
 * Parse one engine frame, or return `null` when it is not a frame this
 * client will act on. The rules are deliberately unforgiving: the keys must
 * be exactly the ones the frame defines, the numbers must be safe integers,
 * the window must fit the engine's bounds, and an output frame's byte
 * interval must match the length of its payload. A frame that fails is
 * dropped, not guessed at — a half-understood frame would move the consumed
 * offset somewhere the engine never was.
 */
export function parseTerminalServerFrame(raw: string): TerminalServerFrame | null {
  if (typeof raw !== 'string') return null
  if (exceedsUtf8Cap(raw, TERMINAL_ENVELOPE_MAX_BYTES)) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const frame = parsed as Record<string, unknown>
  const type = frame.type
  if (typeof type !== 'string' || !Object.hasOwn(FRAME_KEYS, type)) return null
  if (!hasExactKeys(frame, FRAME_KEYS[type])) return null

  switch (type) {
    case 'ready': {
      const sessionId = frame.session_id
      const folder = frame.folder
      const cols = frame.cols
      const rows = frame.rows
      if (!isFilledString(sessionId) || sessionId.length > TERMINAL_MAX_SESSION_ID_CHARS) return null
      if (!isFilledString(folder) || folder.length > TERMINAL_MAX_FOLDER_CHARS) return null
      if (!isCellCount(cols, TERMINAL_MIN_COLS, TERMINAL_MAX_COLS)) return null
      if (!isCellCount(rows, TERMINAL_MIN_ROWS, TERMINAL_MAX_ROWS)) return null
      if (!isOffset(frame.seq) || !isOffset(frame.replay_start)) return null
      if (frame.replay_start > frame.seq) return null
      if (typeof frame.truncated !== 'boolean') return null
      return {
        type: 'ready', session_id: sessionId, folder,
        cols, rows, seq: frame.seq,
        replay_start: frame.replay_start, truncated: frame.truncated,
      }
    }
    case 'output': {
      if (!isOffset(frame.start) || !isOffset(frame.end)) return null
      if (frame.end < frame.start) return null
      if (typeof frame.data !== 'string') return null
      const bytes = decodeTerminalBytes(frame.data)
      if (bytes === null) return null
      if (frame.end - frame.start !== bytes.length) return null
      return { type: 'output', start: frame.start, end: frame.end, data: frame.data }
    }
    case 'replay_end': {
      if (!isOffset(frame.seq)) return null
      if (typeof frame.truncated !== 'boolean') return null
      return { type: 'replay_end', seq: frame.seq, truncated: frame.truncated }
    }
    case 'exit': {
      const code = frame.code
      if (code !== null && !(typeof code === 'number' && Number.isSafeInteger(code))) return null
      return { type: 'exit', code }
    }
    case 'pong':
      return { type: 'pong' }
    case 'error': {
      const reason = frame.reason
      if (typeof reason !== 'string') return null
      if (reason.length > TERMINAL_MAX_REASON_CHARS) return null
      if (reason.trim().length === 0) return null
      return { type: 'error', reason }
    }
    default:
      return null
  }
}

/** Why the terminal socket closed, in the terms the pane can act on. */
export type TerminalCloseReason =
  | 'auth'
  | 'origin'
  | 'gone'
  | 'takeover'
  | 'slow'
  | 'retry'

/**
 * Map a WebSocket close code to what the client should do. The engine uses
 * 4401/4403/4404/4409/4413 for the terminal, and the existing socket
 * authorization still closes with 4001/4003 — both spellings of the same
 * two conditions, because which one arrives depends on which layer refused
 * the connection.
 */
export function terminalCloseReason(code: number): TerminalCloseReason {
  if (code === 4001 || code === 4401) return 'auth'
  if (code === 4003 || code === 4403) return 'origin'
  if (code === 4404) return 'gone'
  if (code === 4409) return 'takeover'
  if (code === 4413) return 'slow'
  return 'retry'
}

/**
 * Backoff between reconnect attempts, in milliseconds. `attempt` is 1-based: the
 * first retry is quick and the delay doubles from there, capped at 5 s.
 */
export function terminalReconnectDelayMs(attempt: number): number {
  if (Number.isNaN(attempt)) return 250
  if (attempt <= 1) return 250
  if (!Number.isFinite(attempt)) return 5000
  const normalized = Math.floor(attempt)
  if (normalized >= 64) return 5000
  return Math.min(250 * 2 ** (normalized - 1), 5000)
}

/**
 * The bytes of an output frame the client has not consumed yet.
 *
 * `consumed` is an absolute byte count — the number of bytes the renderer has
 * been handed, not the index of the last byte. The result is `null` when the
 * frame starts after the cursor (a gap: the client is missing bytes and must
 * not pretend otherwise) or when the cursor is not a usable offset. A frame
 * the cursor has passed yields no bytes and leaves the cursor where it is:
 * replay and live output overlap on reconnect, and writing that overlap twice
 * is how a terminal ends up with duplicated lines.
 */
export function terminalOutputSlice(
  frame: Extract<TerminalServerFrame, { type: 'output' }>,
  consumed: number,
): { bytes: Uint8Array; nextOffset: number } | null {
  if (!Number.isSafeInteger(consumed) || consumed < 0) return null
  if (!frame || typeof frame !== 'object') return null
  const data = (frame as { data?: unknown }).data
  if (typeof data !== 'string') return null
  const start = frame.start
  const end = frame.end
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end)) return null
  if (start < 0 || end < start) return null

  if (end <= consumed) return { bytes: new Uint8Array(0), nextOffset: consumed }
  if (start > consumed) return null

  const bytes = decodeTerminalBytes(data)
  if (bytes === null) return null
  if (bytes.length !== end - start) return null
  return { bytes: bytes.subarray(consumed - start), nextOffset: end }
}
