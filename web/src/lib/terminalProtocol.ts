// The embedded terminal's wire protocol: types, strict validation and
// replay-offset arithmetic.
//
// Pure by design — no Vue, no socket, no storage, no rendering. `lib/chatWs.ts`
// has the same shape: it answers "what does this frame mean?" and leaves
// opening, closing and drawing to whoever owns the socket. A terminal frame
// carries raw bytes, so every rule here is about *bytes*, never about text:
// a byte sequence that is not valid UTF-8 is a normal thing for a shell to
// emit, and a decoder that "fixes" it corrupts the stream.
//
// The limits are the engine's, not the browser's: 256 KiB of replay per
// session, 64 KiB per frame, a 1 MiB envelope and a 20..500 x 5..200 window.
// They are constants rather than settings — a terminal that can ask for more
// than the engine grants is a terminal that can wedge the engine.

/** Replay buffer the engine keeps per session, in bytes. */
export const TERMINAL_REPLAY_MAX_BYTES = 256 * 1024

/**
 * Bytes in a single input or output frame. The engine caps a frame at 64 KiB
 * and a decoded output payload at the same number, so this bounds both
 * directions of the byte stream.
 */
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
 * Frames the engine sends.
 *
 * `ready.seq` is a snapshot of the stream, not an instruction: it says how
 * many bytes exist, never how many the client has consumed.
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
  | {
      type: 'output'
      /** First byte offset covered by `data`. */
      start: number
      /** One past the last byte offset covered by `data`. */
      end: number
      /** Strict standard base64 of the raw bytes. */
      data: string
    }
  | { type: 'replay_end'; seq: number; truncated: boolean }
  | { type: 'exit'; code: number | null }
  | { type: 'pong' }
  | { type: 'error'; reason: string }

/**
 * Frames the client sends. `input.data` is strict standard base64 of the raw
 * bytes, so a keystroke and a pasted blob travel the same way.
 */
export type TerminalClientFrame =
  | { type: 'attach'; offset: number; takeover: boolean }
  | { type: 'input'; data: string }
  | { type: 'resize'; cols: number; rows: number }
  | { type: 'ping' }

const BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'

/** `-1` for every byte that is not in the standard base64 alphabet. */
const BASE64_VALUES = (() => {
  const table = new Int8Array(256).fill(-1)
  for (let i = 0; i < BASE64_ALPHABET.length; i += 1) {
    table[BASE64_ALPHABET.charCodeAt(i)] = i
  }
  return table
})()

const BASE64_PAD = 61 // '='

/** Longest base64 string that can hold a legal frame. */
const MAX_ENCODED_CHARS = Math.ceil(TERMINAL_INPUT_MAX_BYTES / 3) * 4

/**
 * Encode raw bytes as strict standard base64.
 *
 * The result is built in chunks rather than by spreading the buffer into a
 * call — `String.fromCharCode(...bytes)` throws on a 64 KiB buffer, and the
 * buffer this handles is exactly that size.
 */
export function encodeTerminalBytes(bytes: Uint8Array): string {
  const parts: string[] = []
  let chunk = ''
  for (let i = 0; i < bytes.length; i += 3) {
    const first = bytes[i]
    const hasSecond = i + 1 < bytes.length
    const hasThird = i + 2 < bytes.length
    const second = hasSecond ? bytes[i + 1] : 0
    const third = hasThird ? bytes[i + 2] : 0
    chunk += BASE64_ALPHABET[first >> 2]
    chunk += BASE64_ALPHABET[((first & 0x03) << 4) | (second >> 4)]
    if (!hasSecond) {
      chunk += '=='
    } else if (!hasThird) {
      chunk += BASE64_ALPHABET[(second & 0x0f) << 2]
      chunk += '='
    } else {
      chunk += BASE64_ALPHABET[((second & 0x0f) << 2) | (third >> 6)]
      chunk += BASE64_ALPHABET[third & 0x3f]
    }
    if (chunk.length >= 8192) {
      parts.push(chunk)
      chunk = ''
    }
  }
  if (chunk) parts.push(chunk)
  return parts.join('')
}

function base64Value(data: string, index: number): number {
  const code = data.charCodeAt(index)
  if (code > 255) return -1
  return BASE64_VALUES[code]
}

/**
 * Decode strict standard base64 into raw bytes, or `null` when the string is
 * not base64 this decoder is willing to accept.
 *
 * "Strict" means: the standard alphabet only (not base64url), a length that
 * is a multiple of four, padding only at the end of the final group, and
 * trailing bits that a partial group cannot carry set to zero — so every
 * byte sequence has exactly one encoding. Anything else is rejected rather
 * than silently repaired, because a repaired stream is a stream the client
 * and the engine disagree about.
 */
export function decodeTerminalBytes(data: string): Uint8Array | null {
  if (typeof data !== 'string') return null
  if (data.length % 4 !== 0) return null
  if (data.length === 0) return new Uint8Array(0)
  if (data.length > MAX_ENCODED_CHARS) return null

  let padding = 0
  if (data.charCodeAt(data.length - 1) === BASE64_PAD) padding = 1
  if (padding === 1 && data.charCodeAt(data.length - 2) === BASE64_PAD) padding = 2

  const groups = data.length / 4
  const out = new Uint8Array(groups * 3)
  let outLength = 0

  for (let group = 0; group < groups; group += 1) {
    const base = group * 4
    const first = base64Value(data, base)
    const second = base64Value(data, base + 1)
    if (first < 0 || second < 0) return null
    const hasThird = base + 2 < data.length - padding
    const hasFourth = base + 3 < data.length - padding
    const third = hasThird ? base64Value(data, base + 2) : 0
    const fourth = hasFourth ? base64Value(data, base + 3) : 0
    if (third < 0 || fourth < 0) return null
    out[outLength++] = (first << 2) | (second >> 4)
    if (hasThird) out[outLength++] = ((second & 0x0f) << 4) | (third >> 2)
    if (hasFourth) out[outLength++] = ((third & 0x03) << 6) | fourth
    if (group === groups - 1) {
      // A partial final group cannot carry every bit its characters hold.
      // Canonical base64 leaves those bits zero.
      if (padding === 1 && (third & 0x03) !== 0) return null
      if (padding === 2 && (second & 0x0f) !== 0) return null
    }
  }

  if (outLength > TERMINAL_INPUT_MAX_BYTES) return null
  return outLength === out.length ? out : out.subarray(0, outLength)
}

const READY_KEYS = [
  'type',
  'session_id',
  'folder',
  'cols',
  'rows',
  'seq',
  'replay_start',
  'truncated',
] as const
const OUTPUT_KEYS = ['type', 'start', 'end', 'data'] as const
const REPLAY_END_KEYS = ['type', 'seq', 'truncated'] as const
const EXIT_KEYS = ['type', 'code'] as const
const PONG_KEYS = ['type'] as const
const ERROR_KEYS = ['type', 'reason'] as const

function hasExactKeys(frame: Record<string, unknown>, keys: readonly string[]): boolean {
  const own = Object.keys(frame)
  if (own.length !== keys.length) return false
  for (const key of keys) {
    if (!Object.prototype.hasOwnProperty.call(frame, key)) return false
  }
  return true
}

/** A byte offset or stream position: a non-negative safe integer, never a boolean. */
function isOffset(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
}

function isColumnCount(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= TERMINAL_MIN_COLS &&
    value <= TERMINAL_MAX_COLS
  )
}

function isRowCount(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= TERMINAL_MIN_ROWS &&
    value <= TERMINAL_MAX_ROWS
  )
}

function isExitCode(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isSafeInteger(value))
}

/**
 * True when the UTF-8 encoding of `value` is longer than `cap` bytes.
 *
 * The count walks the string and stops as soon as it passes the cap, so a
 * grossly oversized envelope is refused without reading all of it.
 */
function utf8ByteLengthExceeds(value: string, cap: number): boolean {
  let bytes = 0
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i)
    if (code < 0x80) {
      bytes += 1
    } else if (code < 0x800) {
      bytes += 2
    } else if (code >= 0xd800 && code <= 0xdbff && i + 1 < value.length) {
      const next = value.charCodeAt(i + 1)
      if (next >= 0xdc00 && next <= 0xdfff) {
        bytes += 4
        i += 1
      } else {
        bytes += 3
      }
    } else {
      bytes += 3
    }
    if (bytes > cap) return true
  }
  return false
}

/**
 * Parse one engine frame, or return `null` when it is not a frame this
 * client will act on.
 *
 * The rules are deliberately unforgiving: the keys must be exactly the ones
 * the frame defines, the numbers must be safe integers, the window must fit
 * the engine's bounds, and an output frame's byte interval must match the
 * length of its payload. A frame that fails any of them is dropped, not
 * guessed at — the client has a reconnect path, and a half-understood frame
 * would move the consumed offset to a place the engine never was.
 */
export function parseTerminalServerFrame(raw: string): TerminalServerFrame | null {
  if (typeof raw !== 'string') return null
  if (utf8ByteLengthExceeds(raw, TERMINAL_ENVELOPE_MAX_BYTES)) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
  const frame = parsed as Record<string, unknown>

  switch (frame.type) {
    case 'ready': {
      if (!hasExactKeys(frame, READY_KEYS)) return null
      const sessionId = frame.session_id
      const folder = frame.folder
      if (typeof sessionId !== 'string' || sessionId.length > TERMINAL_MAX_SESSION_ID_CHARS) {
        return null
      }
      if (typeof folder !== 'string' || folder.length > TERMINAL_MAX_FOLDER_CHARS) return null
      if (!isColumnCount(frame.cols) || !isRowCount(frame.rows)) return null
      if (!isOffset(frame.seq) || !isOffset(frame.replay_start)) return null
      if (frame.replay_start > frame.seq) return null
      if (typeof frame.truncated !== 'boolean') return null
      return {
        type: 'ready',
        session_id: sessionId,
        folder,
        cols: frame.cols,
        rows: frame.rows,
        seq: frame.seq,
        replay_start: frame.replay_start,
        truncated: frame.truncated,
      }
    }
    case 'output': {
      if (!hasExactKeys(frame, OUTPUT_KEYS)) return null
      if (!isOffset(frame.start) || !isOffset(frame.end)) return null
      if (frame.end < frame.start) return null
      if (typeof frame.data !== 'string') return null
      const bytes = decodeTerminalBytes(frame.data)
      if (bytes === null) return null
      if (frame.end - frame.start !== bytes.length) return null
      return { type: 'output', start: frame.start, end: frame.end, data: frame.data }
    }
    case 'replay_end': {
      if (!hasExactKeys(frame, REPLAY_END_KEYS)) return null
      if (!isOffset(frame.seq)) return null
      if (typeof frame.truncated !== 'boolean') return null
      return { type: 'replay_end', seq: frame.seq, truncated: frame.truncated }
    }
    case 'exit': {
      if (!hasExactKeys(frame, EXIT_KEYS)) return null
      if (!isExitCode(frame.code)) return null
      return { type: 'exit', code: frame.code }
    }
    case 'pong': {
      if (!hasExactKeys(frame, PONG_KEYS)) return null
      return { type: 'pong' }
    }
    case 'error': {
      if (!hasExactKeys(frame, ERROR_KEYS)) return null
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
 * Map a WebSocket close code to what the client should do.
 *
 * The engine uses 4401/4403/4404/4409/4413 for the terminal, and the
 * existing socket authorization still closes with 4001/4003 — both spellings
 * of the same two conditions have to be handled, because which one arrives
 * depends on which layer refused the connection.
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
 * Backoff between reconnect attempts, in milliseconds. `attempt` is 1-based.
 *
 * A terminal is a live stream, so the first retry is quick (250 ms) and the
 * delay doubles from there, capped at 5 s — long enough not to hammer a
 * session that is gone, short enough that a laptop waking from sleep feels
 * alive. A nonsensical attempt count is treated as the first attempt rather
 * than as an invitation to compute an unbounded exponent.
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
 * `consumed` is an absolute byte count — the number of bytes the renderer
 * has been handed, not the index of the last byte. The result is `null` when
 * the frame starts after the cursor (a gap: the client is missing bytes and
 * must not pretend otherwise) or when the cursor is not a usable offset.
 *
 * A frame the cursor has already passed yields no bytes and leaves the
 * cursor where it is: replay and live output overlap on reconnect, and
 * writing the overlap twice is how a terminal ends up with duplicated
 * lines.
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
