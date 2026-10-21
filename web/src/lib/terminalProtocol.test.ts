import { describe, expect, expectTypeOf, it } from 'vitest'
import {
  TERMINAL_ENVELOPE_MAX_BYTES,
  TERMINAL_INPUT_MAX_BYTES,
  TERMINAL_MAX_COLS,
  TERMINAL_MAX_FOLDER_CHARS,
  TERMINAL_MAX_REASON_CHARS,
  TERMINAL_MAX_ROWS,
  TERMINAL_MAX_SESSION_ID_CHARS,
  TERMINAL_MIN_COLS,
  TERMINAL_MIN_ROWS,
  TERMINAL_REPLAY_MAX_BYTES,
  decodeTerminalBytes,
  encodeTerminalBytes,
  parseTerminalServerFrame,
  terminalCloseReason,
  terminalOutputSlice,
  terminalReconnectDelayMs,
  type TerminalServerFrame,
} from './terminalProtocol'

function bytes(...values: number[]): Uint8Array {
  return new Uint8Array(values)
}

function frame(value: unknown): string {
  return JSON.stringify(value)
}

function readyFrame(overrides: Record<string, unknown> = {}): string {
  return frame({
    type: 'ready',
    session_id: 'sess-1',
    folder: '/Users/someone/project',
    cols: 120,
    rows: 40,
    seq: 4096,
    replay_start: 1024,
    truncated: true,
    ...overrides,
  })
}

describe('terminal protocol limits', () => {
  it('pins the byte and size caps the wire contract fixes', () => {
    expect(TERMINAL_REPLAY_MAX_BYTES).toBe(256 * 1024)
    expect(TERMINAL_INPUT_MAX_BYTES).toBe(65536)
    expect(TERMINAL_ENVELOPE_MAX_BYTES).toBe(1024 * 1024)
    expect(TERMINAL_MIN_COLS).toBe(20)
    expect(TERMINAL_MAX_COLS).toBe(500)
    expect(TERMINAL_MIN_ROWS).toBe(5)
    expect(TERMINAL_MAX_ROWS).toBe(200)
  })
})

describe('encodeTerminalBytes', () => {
  it('encodes the RFC 4648 base64 vectors', () => {
    expect(encodeTerminalBytes(new TextEncoder().encode('hello'))).toBe('aGVsbG8=')
    expect(encodeTerminalBytes(bytes(0x00, 0x10, 0x83))).toBe('ABCD')
    expect(encodeTerminalBytes(bytes(0xfb, 0xff))).toBe('+/8=')
  })

  it('encodes an empty buffer as an empty string', () => {
    expect(encodeTerminalBytes(new Uint8Array(0))).toBe('')
  })

  it('preserves every byte value from 0 to 255', () => {
    const all = new Uint8Array(256)
    for (let i = 0; i < all.length; i += 1) all[i] = i

    const decoded = decodeTerminalBytes(encodeTerminalBytes(all))

    expect(decoded).toEqual(all)
  })

  it('preserves bytes that are not valid UTF-8', () => {
    const invalid = bytes(0xc3, 0x28, 0xff, 0xfe)

    const decoded = decodeTerminalBytes(encodeTerminalBytes(invalid))

    expect(decoded).toEqual(invalid)
    // The decoded bytes are not the bytes a UTF-8 decoder would produce from
    // the same code points: the raw stream is preserved, not repaired.
    expect(new TextDecoder('utf-8', { fatal: false }).decode(decoded as Uint8Array)).not.toBe(
      new TextDecoder('utf-8', { fatal: false }).decode(bytes(0xc3, 0xa3, 0xff, 0xfe)),
    )
  })

  it('keeps multi-byte characters intact across encode chunk boundaries', () => {
    // The encoder emits base64 in chunks, so a chunk boundary falls every
    // 6144 raw bytes. The three-byte characters below straddle those
    // boundaries: a chunk that decoded text instead of bytes would split
    // them and lose a byte.
    const buffer = new Uint8Array(TERMINAL_INPUT_MAX_BYTES).fill(0x41)
    for (let boundary = 6144; boundary + 2 < buffer.length; boundary += 6144) {
      buffer[boundary - 1] = 0xe6
      buffer[boundary] = 0xbc
      buffer[boundary + 1] = 0xa2
    }

    const encoded = encodeTerminalBytes(buffer)
    const decoded = decodeTerminalBytes(encoded)

    expect(decoded).toEqual(buffer)
    expect(encoded.length).toBe(Math.ceil(buffer.length / 3) * 4)
  })

  it('never emits padding in the middle of the encoded stream', () => {
    const buffer = new Uint8Array(100_000).fill(0x7a)

    const encoded = encodeTerminalBytes(buffer)

    expect(encoded.replace(/=+$/, '')).not.toContain('=')
  })
})

describe('decodeTerminalBytes', () => {
  it('accepts empty data', () => {
    expect(decodeTerminalBytes('')).toEqual(new Uint8Array(0))
  })

  it('decodes the RFC 4648 base64 vectors', () => {
    expect(decodeTerminalBytes('aGVsbG8=')).toEqual(new TextEncoder().encode('hello'))
    expect(decodeTerminalBytes('ABCD')).toEqual(bytes(0x00, 0x10, 0x83))
    expect(decodeTerminalBytes('+/8=')).toEqual(bytes(0xfb, 0xff))
  })

  it('rejects a length that is not a multiple of four', () => {
    expect(decodeTerminalBytes('a')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8=x')).toBeNull()
  })

  it('rejects unpadded base64', () => {
    expect(decodeTerminalBytes('aGVsbG8')).toBeNull()
    expect(decodeTerminalBytes('AA')).toBeNull()
  })

  it('rejects padding outside the final group', () => {
    expect(decodeTerminalBytes('aG=sbG8=')).toBeNull()
    expect(decodeTerminalBytes('a=G=')).toBeNull()
    expect(decodeTerminalBytes('====')).toBeNull()
    expect(decodeTerminalBytes('A===')).toBeNull()
  })

  it('rejects characters outside the standard alphabet', () => {
    expect(decodeTerminalBytes('aGVsbG8 ')).toBeNull()
    expect(decodeTerminalBytes('aGVs bG8=')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8\n')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8-')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8_')).toBeNull()
    expect(decodeTerminalBytes('aGVsbG8!')).toBeNull()
  })

  it('rejects non-canonical trailing bits', () => {
    // 'AB==' and 'ABC=' carry bits the decoder would silently drop.
    expect(decodeTerminalBytes('AA==')).toEqual(bytes(0x00))
    expect(decodeTerminalBytes('AB==')).toBeNull()
    expect(decodeTerminalBytes('AAA=')).toEqual(bytes(0x00, 0x00))
    expect(decodeTerminalBytes('AAB=')).toBeNull()
  })

  it('accepts data exactly at the frame cap', () => {
    const payload = new Uint8Array(TERMINAL_INPUT_MAX_BYTES).fill(0x5a)

    const decoded = decodeTerminalBytes(encodeTerminalBytes(payload))

    expect(decoded).toHaveLength(TERMINAL_INPUT_MAX_BYTES)
  })

  it('rejects data that decodes to more than the frame cap', () => {
    const payload = new Uint8Array(TERMINAL_INPUT_MAX_BYTES + 1).fill(0x5a)

    expect(decodeTerminalBytes(encodeTerminalBytes(payload))).toBeNull()
    expect(decodeTerminalBytes('A'.repeat(87388))).toBeNull()
  })

  it('rejects a payload that is short enough to encode but long enough to decode over the cap', () => {
    // 87384 characters is the longest string the encoder can produce for a
    // legal frame, yet it decodes to 65538 bytes — two over the cap. The
    // decoded length is what bounds the frame, not the encoded one.
    expect('A'.repeat(87384).length).toBeLessThanOrEqual(87384)
    expect(decodeTerminalBytes('A'.repeat(87384))).toBeNull()
  })
})

describe('parseTerminalServerFrame', () => {
  it('accepts a ready frame', () => {
    const parsed = parseTerminalServerFrame(
      frame({
        type: 'ready',
        session_id: 'sess-1',
        folder: '/Users/someone/project',
        cols: 120,
        rows: 40,
        seq: 4096,
        replay_start: 1024,
        truncated: true,
      }),
    )

    expect(parsed).toEqual({
      type: 'ready',
      session_id: 'sess-1',
      folder: '/Users/someone/project',
      cols: 120,
      rows: 40,
      seq: 4096,
      replay_start: 1024,
      truncated: true,
    })
  })

  it('accepts an output frame', () => {
    const parsed = parseTerminalServerFrame(
      frame({ type: 'output', start: 10, end: 12, data: 'aGk=' }),
    )

    expect(parsed).toEqual({ type: 'output', start: 10, end: 12, data: 'aGk=' })
  })

  it('rejects a ready frame with an empty session id', () => {
    expect(parseTerminalServerFrame(readyFrame({ session_id: '' }))).toBeNull()
  })

  it('rejects a ready frame with a whitespace-only session id', () => {
    expect(parseTerminalServerFrame(readyFrame({ session_id: '   ' }))).toBeNull()
    expect(parseTerminalServerFrame(readyFrame({ session_id: '\t\n' }))).toBeNull()
  })

  it('rejects a ready frame with an empty folder', () => {
    expect(parseTerminalServerFrame(readyFrame({ folder: '' }))).toBeNull()
  })

  it('rejects a ready frame with a whitespace-only folder', () => {
    expect(parseTerminalServerFrame(readyFrame({ folder: '   ' }))).toBeNull()
    expect(parseTerminalServerFrame(readyFrame({ folder: '\t' }))).toBeNull()
  })

  it('accepts a folder that legitimately contains spaces', () => {
    const parsed = parseTerminalServerFrame(
      readyFrame({ folder: '/Users/someone/My Project' }),
    )

    expect(parsed).not.toBeNull()
    expect(parsed!.type === 'ready' && parsed!.folder).toBe('/Users/someone/My Project')
  })

  it('accepts a session id that legitimately contains spaces', () => {
    const parsed = parseTerminalServerFrame(readyFrame({ session_id: 'sess 1' }))

    expect(parsed).not.toBeNull()
    expect(parsed!.type === 'ready' && parsed!.session_id).toBe('sess 1')
  })

  it('accepts a replay_end frame', () => {
    const parsed = parseTerminalServerFrame(frame({ type: 'replay_end', seq: 90, truncated: false }))

    expect(parsed).toEqual({ type: 'replay_end', seq: 90, truncated: false })
  })

  it('accepts an exit frame with a code and with null', () => {
    expect(parseTerminalServerFrame(frame({ type: 'exit', code: 0 }))).toEqual({
      type: 'exit',
      code: 0,
    })
    expect(parseTerminalServerFrame(frame({ type: 'exit', code: null }))).toEqual({
      type: 'exit',
      code: null,
    })
  })

  it('accepts a pong frame', () => {
    expect(parseTerminalServerFrame(frame({ type: 'pong' }))).toEqual({ type: 'pong' })
  })

  it('accepts an error frame', () => {
    expect(parseTerminalServerFrame(frame({ type: 'error', reason: 'session gone' }))).toEqual({
      type: 'error',
      reason: 'session gone',
    })
  })

  it('rejects an unknown frame type', () => {
    expect(parseTerminalServerFrame(frame({ type: 'nope' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: '' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 7 }))).toBeNull()
    expect(parseTerminalServerFrame(frame({}))).toBeNull()
  })

  it('rejects a frame type that is an inherited Object.prototype key without throwing', () => {
    // The frame-type table is a plain object, so a type like "constructor"
    // resolves to an inherited Object.prototype function. An unknown type
    // must be refused, never dispatched on.
    const inherited = [
      'constructor',
      'hasOwnProperty',
      '__proto__',
      'toString',
      'valueOf',
      'isPrototypeOf',
      'propertyIsEnumerable',
      'toLocaleString',
    ]

    for (const type of inherited) {
      expect(parseTerminalServerFrame(frame({ type }))).toBeNull()
    }
  })

  it('rejects a frame carrying an inherited Object.prototype key', () => {
    // The same names as *frame keys* are extra keys, so the frame is not the
    // frame it claims to be. `__proto__` is written as raw JSON because a
    // JSON literal sets the prototype instead of adding a key.
    expect(parseTerminalServerFrame('{"type":"pong","constructor":1}')).toBeNull()
    expect(parseTerminalServerFrame('{"type":"pong","hasOwnProperty":1}')).toBeNull()
    expect(parseTerminalServerFrame('{"type":"pong","__proto__":1}')).toBeNull()
    expect(parseTerminalServerFrame('{"type":"error","reason":"x","toString":1}')).toBeNull()
  })

  it('rejects malformed JSON and non-object payloads', () => {
    expect(parseTerminalServerFrame('not json')).toBeNull()
    expect(parseTerminalServerFrame('')).toBeNull()
    expect(parseTerminalServerFrame('null')).toBeNull()
    expect(parseTerminalServerFrame('123')).toBeNull()
    expect(parseTerminalServerFrame('"pong"')).toBeNull()
    expect(parseTerminalServerFrame('[{"type":"pong"}]')).toBeNull()
  })

  it('rejects a frame with a missing key', () => {
    expect(
      parseTerminalServerFrame(
        frame({ type: 'ready', session_id: 's', folder: '/tmp', cols: 80, rows: 24, seq: 1 }),
      ),
    ).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 1 }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'error' }))).toBeNull()
  })

  it('rejects a frame with an extra key', () => {
    expect(parseTerminalServerFrame(frame({ type: 'pong', extra: 1 }))).toBeNull()
    expect(
      parseTerminalServerFrame(
        frame({ type: 'exit', code: 0, detail: 'bye' }),
      ),
    ).toBeNull()
  })

  it('rejects a frame with a wrong field type', () => {
    expect(parseTerminalServerFrame(frame({ type: 'error', reason: 42 }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: '0', end: 1, data: 'aGk=' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 1, data: 7 }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'replay_end', seq: 1, truncated: 'yes' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'exit', code: '0' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'exit', code: 1.5 }))).toBeNull()
  })

  it('rejects a boolean where a number is required', () => {
    expect(
      parseTerminalServerFrame(
        frame({
          type: 'ready',
          session_id: 's',
          folder: '/tmp',
          cols: true,
          rows: 24,
          seq: 1,
          replay_start: 0,
          truncated: false,
        }),
      ),
    ).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: false, end: 1, data: 'aGk=' }))).toBeNull()
  })

  it('rejects NaN, non-integer and unsafe-integer offsets', () => {
    expect(parseTerminalServerFrame(frame({ type: 'output', start: NaN, end: 1, data: 'aGk=' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 1.5, data: 'aGk=' }))).toBeNull()
    expect(
      parseTerminalServerFrame(
        frame({ type: 'output', start: 0, end: Number.MAX_SAFE_INTEGER + 2, data: 'aGk=' }),
      ),
    ).toBeNull()
    expect(
      parseTerminalServerFrame(
        frame({ type: 'replay_end', seq: Number.MAX_SAFE_INTEGER + 2, truncated: false }),
      ),
    ).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: -1, end: 1, data: 'aGk=' }))).toBeNull()
  })

  it('rejects an inverted output interval', () => {
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 10, end: 4, data: 'aGk=' }))).toBeNull()
  })

  it('rejects an output frame whose payload length does not match its interval', () => {
    // 'aGk=' decodes to two bytes.
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 1, data: 'aGk=' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 3, data: 'aGk=' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'output', start: 0, end: 2, data: 'not base64' }))).toBeNull()
  })

  it('rejects a ready frame whose replay range is inverted', () => {
    expect(
      parseTerminalServerFrame(
        frame({
          type: 'ready',
          session_id: 's',
          folder: '/tmp',
          cols: 80,
          rows: 24,
          seq: 10,
          replay_start: 11,
          truncated: false,
        }),
      ),
    ).toBeNull()
  })

  it('rejects a size outside the terminal bounds', () => {
    const ready = (cols: number, rows: number) =>
      parseTerminalServerFrame(
        frame({
          type: 'ready',
          session_id: 's',
          folder: '/tmp',
          cols,
          rows,
          seq: 0,
          replay_start: 0,
          truncated: false,
        }),
      )

    expect(ready(TERMINAL_MIN_COLS - 1, 24)).toBeNull()
    expect(ready(TERMINAL_MAX_COLS + 1, 24)).toBeNull()
    expect(ready(80, TERMINAL_MIN_ROWS - 1)).toBeNull()
    expect(ready(80, TERMINAL_MAX_ROWS + 1)).toBeNull()
    expect(ready(TERMINAL_MIN_COLS, TERMINAL_MIN_ROWS)).not.toBeNull()
    expect(ready(TERMINAL_MAX_COLS, TERMINAL_MAX_ROWS)).not.toBeNull()
  })

  it('rejects fields that are longer than their bound', () => {
    expect(
      parseTerminalServerFrame(frame({ type: 'error', reason: 'x'.repeat(TERMINAL_MAX_REASON_CHARS + 1) })),
    ).toBeNull()
    expect(
      parseTerminalServerFrame(
        frame({
          type: 'ready',
          session_id: 'x'.repeat(TERMINAL_MAX_SESSION_ID_CHARS + 1),
          folder: '/tmp',
          cols: 80,
          rows: 24,
          seq: 0,
          replay_start: 0,
          truncated: false,
        }),
      ),
    ).toBeNull()
    expect(
      parseTerminalServerFrame(
        frame({
          type: 'ready',
          session_id: 's',
          folder: 'x'.repeat(TERMINAL_MAX_FOLDER_CHARS + 1),
          cols: 80,
          rows: 24,
          seq: 0,
          replay_start: 0,
          truncated: false,
        }),
      ),
    ).toBeNull()
  })

  it('rejects an empty reason', () => {
    expect(parseTerminalServerFrame(frame({ type: 'error', reason: '' }))).toBeNull()
    expect(parseTerminalServerFrame(frame({ type: 'error', reason: '   ' }))).toBeNull()
  })

  it('rejects an envelope larger than the envelope cap', () => {
    // Trailing whitespace is legal JSON, so this frame is well-formed: only
    // the envelope cap can refuse it.
    const padded = frame({ type: 'error', reason: 'x'.repeat(TERMINAL_MAX_REASON_CHARS) })
    expect(parseTerminalServerFrame(padded)).not.toBeNull()
    expect(parseTerminalServerFrame(padded + ' '.repeat(TERMINAL_ENVELOPE_MAX_BYTES))).toBeNull()
  })

  it('rejects an envelope whose UTF-8 bytes exceed the cap even though its UTF-16 length does not', () => {
    // A 3-byte character costs three UTF-8 bytes but one UTF-16 code unit, so
    // a string can be well under the cap in characters and well over it in
    // bytes. Counting characters alone would let it through.
    //
    // The frame is *valid JSON* on purpose. A duplicate "type" key is legal
    // JSON and the last one wins, so the frame really is a pong frame -- but
    // the bytes that carry it are over the cap. The envelope cap is then the
    // only thing that can refuse it. A multi-byte blob that is not JSON at
    // all would be rejected by the parser for a different reason, and this
    // test would pass even with a character-only cap.
    const characters = Math.floor(TERMINAL_ENVELOPE_MAX_BYTES / 3) + 10
    const multibyte = `{"type":"pong","type":"${'€'.repeat(characters)}","type":"pong"}`

    expect(multibyte.length).toBeLessThan(TERMINAL_ENVELOPE_MAX_BYTES)
    expect(new TextEncoder().encode(multibyte).length).toBeGreaterThan(
      TERMINAL_ENVELOPE_MAX_BYTES,
    )
    // The bytes are valid JSON that really is a pong frame: only the cap can
    // refuse this.
    expect(JSON.parse(multibyte)).toEqual({ type: 'pong' })
    expect(parseTerminalServerFrame(multibyte)).toBeNull()
  })

  it('accepts a multi-byte envelope that is under the cap in bytes', () => {
    const multibyte = `{"type":"error","reason":"${'€'.repeat(32)}"}`

    expect(new TextEncoder().encode(multibyte).length).toBeLessThan(
      TERMINAL_ENVELOPE_MAX_BYTES,
    )
    expect(parseTerminalServerFrame(multibyte)).not.toBeNull()
  })

  it('rejects a frame that is not a plain object', () => {
    expect(parseTerminalServerFrame('null')).toBeNull()
    expect(parseTerminalServerFrame('true')).toBeNull()
    expect(parseTerminalServerFrame('[]')).toBeNull()
  })
})

describe('terminalCloseReason', () => {
  it('maps the existing auth and policy close codes', () => {
    expect(terminalCloseReason(4001)).toBe('auth')
    expect(terminalCloseReason(4003)).toBe('origin')
  })

  it('maps the terminal-specific close codes', () => {
    expect(terminalCloseReason(4401)).toBe('auth')
    expect(terminalCloseReason(4403)).toBe('origin')
    expect(terminalCloseReason(4404)).toBe('gone')
    expect(terminalCloseReason(4409)).toBe('takeover')
    expect(terminalCloseReason(4413)).toBe('slow')
  })

  it('retries every other close code', () => {
    expect(terminalCloseReason(1000)).toBe('retry')
    expect(terminalCloseReason(1006)).toBe('retry')
    expect(terminalCloseReason(0)).toBe('retry')
    expect(terminalCloseReason(-1)).toBe('retry')
    expect(terminalCloseReason(Number.NaN)).toBe('retry')
  })
})

describe('terminalReconnectDelayMs', () => {
  it('starts at 250ms and doubles', () => {
    expect(terminalReconnectDelayMs(1)).toBe(250)
    expect(terminalReconnectDelayMs(2)).toBe(500)
    expect(terminalReconnectDelayMs(3)).toBe(1000)
    expect(terminalReconnectDelayMs(4)).toBe(2000)
    expect(terminalReconnectDelayMs(5)).toBe(4000)
  })

  it('clamps a negative attempt to the first retry', () => {
    expect(terminalReconnectDelayMs(0)).toBe(250)
    expect(terminalReconnectDelayMs(-10)).toBe(250)
    expect(terminalReconnectDelayMs(Number.NaN)).toBe(250)
  })

  it('caps the delay for normal and pathological attempts', () => {
    expect(terminalReconnectDelayMs(6)).toBe(5000)
    expect(terminalReconnectDelayMs(20)).toBe(5000)
    expect(terminalReconnectDelayMs(1_000_000)).toBe(5000)
    expect(terminalReconnectDelayMs(Number.POSITIVE_INFINITY)).toBe(5000)
  })
})

describe('terminalOutputSlice', () => {
  const output = (
    start: number,
    end: number,
    text: string,
  ): Extract<TerminalServerFrame, { type: 'output' }> => ({
    type: 'output',
    start,
    end,
    data: encodeTerminalBytes(new TextEncoder().encode(text)),
  })

  it('returns the whole frame for an exact continuation', () => {
    const slice = terminalOutputSlice(output(10, 13, 'hi\n'), 10)

    expect(slice).not.toBeNull()
    expect(new TextDecoder().decode(slice!.bytes)).toBe('hi\n')
    expect(slice!.nextOffset).toBe(13)
  })

  it('returns nothing for a full duplicate', () => {
    const slice = terminalOutputSlice(output(10, 13, 'hi\n'), 13)

    expect(slice).not.toBeNull()
    expect(slice!.bytes).toHaveLength(0)
    expect(slice!.nextOffset).toBe(13)
  })

  it('leaves the cursor where it is when the frame is entirely behind it', () => {
    const slice = terminalOutputSlice(output(10, 13, 'hi\n'), 20)

    expect(slice).not.toBeNull()
    expect(slice!.bytes).toHaveLength(0)
    expect(slice!.nextOffset).toBe(20)
  })

  it('trims the overlap of a partial duplicate', () => {
    const slice = terminalOutputSlice(output(10, 13, 'hi\n'), 11)

    expect(slice).not.toBeNull()
    expect(new TextDecoder().decode(slice!.bytes)).toBe('i\n')
    expect(slice!.nextOffset).toBe(13)
  })

  it('reports a gap when the frame starts after the consumed offset', () => {
    expect(terminalOutputSlice(output(20, 23, 'hi\n'), 10)).toBeNull()
  })

  it('refuses an invalid consumed cursor', () => {
    const frame = output(10, 13, 'hi\n')

    expect(terminalOutputSlice(frame, -1)).toBeNull()
    expect(terminalOutputSlice(frame, 1.5)).toBeNull()
    expect(terminalOutputSlice(frame, Number.NaN)).toBeNull()
    expect(terminalOutputSlice(frame, Number.POSITIVE_INFINITY)).toBeNull()
  })

  it('never advances the consumed cursor from a ready frame', () => {
    const ready: TerminalServerFrame = {
      type: 'ready',
      session_id: 'sess-1',
      folder: '/tmp',
      cols: 80,
      rows: 24,
      seq: 42,
      replay_start: 0,
      truncated: false,
    }

    // The type boundary: a ready frame is not an output frame, so a consumer
    // cannot hand `ready.seq` to this helper as a consumed offset.
    expectTypeOf(ready).not.toExtend<Extract<TerminalServerFrame, { type: 'output' }>>()

    // And at runtime a ready frame carries no byte interval, so nothing is
    // ever applied from one.
    const asOutput = ready as unknown as Extract<TerminalServerFrame, { type: 'output' }>
    expect(terminalOutputSlice(asOutput, 0)).toBeNull()
  })
})
