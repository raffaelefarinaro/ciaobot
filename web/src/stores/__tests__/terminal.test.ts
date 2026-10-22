// @vitest-environment jsdom

/**
 * `stores/terminal.ts` — the browser half of the embedded terminal.
 *
 * The store owns four things the pane must not get wrong on its own: the
 * HTTP actions against the pinned wire contract, the socket lifecycle, the
 * bounded renderer backpressure and the replay offset.
 *
 * The offset is the heart of it. `ready.seq` says how many bytes exist; it is
 * never the number of bytes this browser consumed. The consumed offset moves
 * only after the renderer's write promise resolves, so a reconnect resumes
 * from the screen the user actually has — and a reconnect never starts a
 * second shell.
 *
 * Everything here is driven by fakes: a fake API, a fake WebSocket and a
 * fake renderer. No engine, no network, no browser.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useTerminalStore, type TerminalRenderer } from '../terminal'
import { encodeTerminalBytes } from '../../lib/terminalProtocol'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: apiDel },
}))

// ── Fakes ────────────────────────────────────────────────────────────────

let fakeSockets: FakeWebSocket[] = []

class FakeWebSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3
  /** When false the handshake never completes, as a refused handshake does. */
  static autoOpen = true

  readyState = FakeWebSocket.CONNECTING
  bufferedAmount = 0
  sent: string[] = []
  closed = false

  #onopen: (() => void) | null = null
  get onopen() { return this.#onopen }
  set onopen(fn: (() => void) | null) {
    this.#onopen = fn
    if (fn && FakeWebSocket.autoOpen) {
      this.readyState = FakeWebSocket.OPEN
      fn()
    }
  }

  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(public url: string) {
    fakeSockets.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close(code?: number) {
    this.closed = true
    this.readyState = FakeWebSocket.CLOSED
    this.onclose?.({ code } as CloseEvent)
  }

  /** Complete the handshake by hand. */
  handshake() {
    this.readyState = FakeWebSocket.OPEN
    this.onopen?.()
  }

  /**
   * Answer the attach with the terminal's ready frame.
   *
   * The transport being open is not the terminal being live: the engine says
   * the session is ready in its own frame, and the client waits for it.
   */
  ready(sessionId = 's1', overrides: Record<string, unknown> = {}) {
    this.receive({
      type: 'ready', session_id: sessionId, folder: '/tmp', cols: 80, rows: 24,
      seq: 0, replay_start: 0, truncated: false,
      ...overrides,
    })
  }

  /** Deliver one server frame. */
  receive(frame: unknown) {
    const data = typeof frame === 'string' ? frame : JSON.stringify(frame)
    this.onmessage?.({ data })
  }

  frames(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>)
  }

  ofType(type: string): Record<string, unknown>[] {
    return this.frames().filter((frame) => frame.type === type)
  }
}

interface FakeRenderer {
  renderer: TerminalRenderer
  writes: Uint8Array[]
  resets: number
  /** Resolve the write that is waiting, if any. */
  settle(): void
}

function makeRenderer(sessionId: string, deferred = false): FakeRenderer {
  const writes: Uint8Array[] = []
  let waiting: (() => void) | null = null
  const fake: FakeRenderer = {
    writes,
    resets: 0,
    settle() {
      const resolve = waiting
      waiting = null
      resolve?.()
    },
    renderer: {
      sessionId,
      reset() {
        fake.resets += 1
      },
      write(bytes: Uint8Array) {
        writes.push(bytes)
        if (!deferred) return Promise.resolve()
        return new Promise<void>((resolve) => {
          waiting = resolve
        })
      },
    },
  }
  return fake
}

function statusPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    available: true,
    reason: '',
    platform: 'darwin',
    host: 'engine.local',
    unlock_expires_in: 900,
    detached_ttl_seconds: 900,
    ...overrides,
  }
}

function sessionPayload(id: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: id,
    folder: '/Users/someone/project',
    cols: 80,
    rows: 24,
    seq: 0,
    attached: false,
    exited: false,
    exit_code: null,
    ...overrides,
  }
}

function b64(text: string): string {
  return encodeTerminalBytes(new TextEncoder().encode(text))
}

function deferred<T>(): {
  promise: Promise<T>
  resolve: (value: T) => void
  reject: (reason?: unknown) => void
} {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function apiError(status: number, message = 'refused'): Error {
  return Object.assign(new Error(message), { status })
}

// Pinia does not dispose the instance it replaces, so without this every
// store a test creates stays alive for the rest of the file — including the
// window listeners it registered, which then answer the events the next test
// dispatches and open sockets the test never asked for.
type DisposablePinia = { _s: Map<string, { $dispose: () => void }> }
const piniasCreated: DisposablePinia[] = []

function newPinia(): ReturnType<typeof createPinia> {
  const pinia = createPinia()
  piniasCreated.push(pinia as unknown as DisposablePinia)
  return pinia
}

beforeEach(() => {
  setActivePinia(newPinia())
  fakeSockets = []
  FakeWebSocket.autoOpen = true
  apiGet.mockReset()
  apiPost.mockReset()
  apiDel.mockReset()
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
    if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
    return Promise.resolve({})
  })
  vi.stubGlobal('WebSocket', FakeWebSocket)
  vi.useFakeTimers()
})

afterEach(() => {
  for (const pinia of piniasCreated.splice(0)) {
    pinia._s.forEach((store) => store.$dispose())
  }
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('primary review regressions', () => {
  it('refuses a rebound renderer without letting an old reset corrupt a successor', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    const events: string[] = []
    const write = view.renderer.write
    view.renderer.write = (bytes) => {
      events.push(new TextDecoder().decode(bytes))
      return write(bytes)
    }
    view.renderer.reset = () => { events.push('reset') }
    store.attachRenderer(view.renderer)
    const first = fakeSockets.at(-1)!
    first.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    first.ready('s1', { seq: 5, replay_start: 2, truncated: true })
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    ;(view.renderer as { sessionId: string }).sessionId = 's2'
    store.detachRenderer(view.renderer)
    store.attachRenderer(view.renderer)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(events).toEqual(['hello', 'reset'])
    expect(store.error).toContain('cannot be reused')
  })

  it('does not render the in-flight prefix twice when hidden and resumed before acknowledgement', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    fakeSockets.at(-1)!.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    fakeSockets.at(-1)!.receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    store.hide()
    await store.open({ chatId: 'chat-1' })
    const resumed = fakeSockets.at(-1)!
    expect(resumed.ofType('attach')[0]?.offset).toBe(0)
    resumed.receive({ type: 'output', start: 0, end: 10, data: b64('helloworld') })
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(view.writes.map((bytes) => new TextDecoder().decode(bytes))).toEqual(['hello', 'world'])
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
  })

  it('keeps the in-flight bytes in the output bound after hiding', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const first = fakeSockets.at(-1)!
    const chunk = new Uint8Array(64 * 1024)
    first.receive({ type: 'output', start: 0, end: chunk.length, data: encodeTerminalBytes(chunk) })
    first.receive({
      type: 'output', start: chunk.length, end: chunk.length * 2, data: encodeTerminalBytes(chunk),
    })

    // Hiding discards only the queued tail. The prefix is already being
    // written and must remain in the bound until that write settles.
    store.hide()
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.open({ chatId: 'chat-1' })
    const resumed = fakeSockets.at(-1)!
    expect(resumed.ofType('attach')[0]?.offset).toBe(chunk.length)

    // Sixteen 64 KiB chunks fit exactly. The seventeenth must not fit; a
    // hidden in-flight prefix must not have made the accounting negative.
    for (let i = 1; i <= 17; i += 1) {
      resumed.receive({
        type: 'output', start: i * chunk.length, end: (i + 1) * chunk.length,
        data: encodeTerminalBytes(chunk),
      })
    }
    await vi.advanceTimersByTimeAsync(0)

    expect(store.error).toContain('faster than the terminal')
  })

  it('does not reset a detached renderer while its prior write is still pending', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const ws = fakeSockets.at(-1)!
    ws.receive({ type: 'output', start: 0, end: 10, data: b64('helloworld') })
    ws.receive({ type: 'ready', session_id: 's1', folder: '/repo', cols: 80, rows: 24,
      seq: 10, replay_start: 5, truncated: true })
    store.detachRenderer(view.renderer)
    expect(view.resets).toBe(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(view.resets).toBe(1)
  })

  it('keeps a confirmed renderer offset when another session is selected and then leaves', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    fakeSockets.at(-1)!.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.selectSession('s1')
    expect(fakeSockets.at(-1)?.ofType('attach')[0]?.offset).toBe(5)
  })

  it('replays a queued tail discarded by hide instead of silently dropping it', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const first = fakeSockets.at(-1)!
    first.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    first.receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    store.hide()
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.open({ chatId: 'chat-1' })
    const resumed = fakeSockets.at(-1)!
    expect(resumed.ofType('attach')[0]?.offset).toBe(5)
    resumed.receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    expect(view.writes.map((bytes) => new TextDecoder().decode(bytes))).toEqual(['hello', 'world'])
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
  })

  it('does not acknowledge duplicate output before the renderer resolves', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const output = { type: 'output', start: 0, end: 5, data: b64('hello') }
    fakeSockets[0].receive(output)
    fakeSockets[0].receive(output)
    await store.reconnect()
    expect(fakeSockets.at(-1)?.ofType('attach')[0]?.offset).toBe(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
  })

  it('does not close the successor socket when an older End request completes', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    const ending = deferred<unknown>()
    apiDel.mockReturnValueOnce(ending.promise)
    const request = store.endSession()
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s2').renderer)
    const successor = fakeSockets.at(-1)
    ending.resolve({ ok: true })
    await request
    expect(store.activeSession?.session_id).toBe('s2')
    expect(successor?.closed).toBe(false)
  })

  it('limits reconnects that open but never complete the terminal ready handshake', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    for (let attempt = 0; attempt < 10; attempt += 1) {
      fakeSockets.at(-1)?.close(1006)
      await vi.advanceTimersByTimeAsync(5000)
    }
    expect(fakeSockets.length).toBeLessThanOrEqual(9)
    expect(store.phase).toBe('error')
  })
})

// ── Ownership races ──────────────────────────────────────────────────────
//
// The store owns one connection and one surface at a time. These are the
// races where the old one answers after the new one took over: a late write,
// a late close, a late HTTP reply. None of them may touch the successor.

describe('terminal store ownership races', () => {
  async function started() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    return store
  }

  it('renders the replayed bytes once when a reconnect races an old write', async () => {
    const store = await started()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const first = fakeSockets[0]
    first.ready()
    first.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The connection is replaced while the first write is still in flight.
    // The engine replays the same five bytes from the acknowledged offset.
    await store.reconnect()
    const second = fakeSockets[1]
    second.ready()
    second.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The old write resolves now. The bytes must reach the screen once, not
    // twice, and the acknowledged cursor must match what was rendered.
    view.settle()
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(1)
    expect(new TextDecoder().decode(view.writes[0])).toBe('hello')
    await store.reconnect()
    expect(fakeSockets[2].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
  })

  it('leaves the successor live when an old write fails', async () => {
    const store = await started()
    const failing: { fail: () => void } = { fail: () => {} }
    const view = {
      sessionId: 's1',
      reset() {},
      write() {
        return new Promise<void>((_resolve, reject) => {
          failing.fail = () => reject(new Error('write failed'))
        })
      },
    }
    store.attachRenderer(view)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // A new surface takes over before the old write fails.
    const successor = makeRenderer('s1')
    store.attachRenderer(successor.renderer)
    const live = fakeSockets[fakeSockets.length - 1]
    failing.fail()
    await vi.advanceTimersByTimeAsync(0)

    // The failure belonged to the old surface. The successor is live.
    expect(store.phase).not.toBe('error')
    expect(store.error).not.toContain('could not be written')
    expect(live.closed).toBe(false)
  })

  it('respects the acknowledged bytes when the same surface is detached and reattached', async () => {
    const store = await started()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The surface is detached and comes back while its write is in flight.
    store.detachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)
    store.attachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    // The write did complete, and that surface is the one showing the bytes,
    // so the resume point is the offset it acknowledged — not zero, and not
    // an offset from a stream that was never rendered.
    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
    // The bytes reached the screen once.
    expect(view.writes).toHaveLength(1)
  })

  it('does not let a delayed End rejection write an error over the successor', async () => {
    const store = await started()
    store.attachRenderer(makeRenderer('s1').renderer)
    const ending = deferred<unknown>()
    apiDel.mockReturnValueOnce(ending.promise)
    const request = store.endSession()

    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s2').renderer)

    ending.reject(new Error('network down'))
    expect(await request).toBe(false)

    // The failure belongs to the session that was ended. The successor is
    // not in error, and the pane does not say the end failed.
    expect(store.activeSession?.session_id).toBe('s2')
    expect(store.phase).not.toBe('error')
    expect(store.error).toBe('')
  })

  it('does not let a delayed malformed End reply write an error over the successor', async () => {
    const store = await started()
    store.attachRenderer(makeRenderer('s1').renderer)
    const ending = deferred<unknown>()
    apiDel.mockReturnValueOnce(ending.promise)
    const request = store.endSession()

    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s2').renderer)

    ending.resolve({ not_ok: true })
    expect(await request).toBe(false)

    expect(store.activeSession?.session_id).toBe('s2')
    expect(store.phase).not.toBe('error')
    expect(store.error).toBe('')
  })

  it('keeps a confirmed renderer offset when another session is selected', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    fakeSockets.at(-1)!.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // Another session is selected, and then this one again. The surface
    // still shows what it showed.
    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.selectSession('s1')

    expect(fakeSockets.at(-1)?.ofType('attach')[0]?.offset).toBe(5)
  })

  it('refuses a renderer rebound to another session without opening a socket', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    fakeSockets.at(-1)!.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    expect(view.writes).toHaveLength(1)

    // A physical screen cannot remember two sessions. Rebinding it at runtime
    // is refused rather than resuming an offset that belongs to s1.
    apiPost.mockResolvedValue({ session: sessionPayload('s2') })
    await store.createSession()
    ;(view.renderer as { sessionId: string }).sessionId = 's2'
    store.detachRenderer(view.renderer)
    store.attachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)

    expect(fakeSockets).toHaveLength(1)
    expect(store.error).toContain('cannot be reused')
    expect(view.writes.map((bytes) => new TextDecoder().decode(bytes))).toEqual(['hello'])
  })

  it('does not write the queued bytes of one surface to another surface', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1', true)
    store.attachRenderer(first.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    // The second write is queued behind the first, which never confirms.
    fakeSockets[0].receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)

    // Another surface takes over. The queued bytes belong to the first one.
    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    await vi.advanceTimersByTimeAsync(0)
    first.settle()
    await vi.advanceTimersByTimeAsync(0)

    expect(second.writes).toHaveLength(0)
    expect(first.writes.map((bytes) => new TextDecoder().decode(bytes)))
      .toEqual(['hello', 'world'])
  })

  it('does not let a rebound renderer start successor output while an old write is pending', async () => {
    const store = await started()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    ;(view.renderer as { sessionId: string }).sessionId = 's2'
    store.detachRenderer(view.renderer)
    store.attachRenderer(view.renderer)

    // No s2 connection is opened, so no s2 output can race the old write.
    expect(view.writes.map((bytes) => new TextDecoder().decode(bytes))).toEqual(['hello'])
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(view.writes.map((bytes) => new TextDecoder().decode(bytes))).toEqual(['hello'])
    expect(store.error).toContain('cannot be reused')
  })

  it('does not let a stale open of the same scope land on a newer one', async () => {
    const store = useTerminalStore()
    const older = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return older.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const first = store.open({ chatId: 'chat-1' })

    // The pane is opened again for the same scope.
    const newer = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return newer.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const second = store.open({ chatId: 'chat-1' })

    newer.resolve(statusPayload({ host: 'new-host' }))
    await second
    older.resolve(statusPayload({ host: 'stale-host' }))
    await first

    // The newer open answers: the stale one is about a pane that is gone.
    expect(store.status?.host).toBe('new-host')
  })

  it('does not let a stale open failure write an error over a newer one', async () => {
    const store = useTerminalStore()
    const older = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return older.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const first = store.open({ chatId: 'chat-1' })

    const newer = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return newer.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const second = store.open({ chatId: 'chat-1' })

    newer.resolve(statusPayload())
    await second
    older.reject(new Error('network down'))
    await first

    // The stale failure is not the pane's error to show.
    expect(store.error).toBe('')
    expect(store.phase).not.toBe('error')
  })

  it('does not let a stale list failure write an error over a newer one', async () => {
    const store = await started()
    const older = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return older.promise
      return Promise.resolve({})
    })
    const first = store.refreshSessions()

    const newer = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return newer.promise
      return Promise.resolve({})
    })
    const second = store.refreshSessions()

    newer.resolve({ sessions: [sessionPayload('s1')] })
    await second
    older.reject(new Error('network down'))
    await first

    expect(store.error).toBe('')
    expect(store.sessions).toHaveLength(1)
  })

  it('does not let a delayed End rejection touch the successor', async () => {
    const store = await started()
    store.attachRenderer(makeRenderer('s1').renderer)
    const ending = deferred<unknown>()
    apiDel.mockReturnValueOnce(ending.promise)
    const request = store.endSession()

    apiPost.mockResolvedValueOnce({ session: sessionPayload('s2') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s2').renderer)
    const successor = fakeSockets.at(-1)

    ending.reject(new Error('network down'))
    expect(await request).toBe(false)

    expect(store.activeSession?.session_id).toBe('s2')
    expect(successor?.closed).toBe(false)
    expect(store.phase).not.toBe('error')
  })

  it('does not let an overlapping list reply change the selection', async () => {
    const store = await started()
    apiPost.mockResolvedValue({ session: sessionPayload('s2') })
    await store.createSession()
    expect(store.activeSession?.session_id).toBe('s2')

    // Two lists in flight; the older one answers last.
    const older = deferred<unknown>()
    const newer = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return older.promise
      return Promise.resolve({})
    })
    const first = store.refreshSessions()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return newer.promise
      return Promise.resolve({})
    })
    const second = store.refreshSessions()

    newer.resolve({ sessions: [sessionPayload('s2'), sessionPayload('s3')] })
    await second
    older.resolve({ sessions: [sessionPayload('s2')] })
    await first

    // The older list updates the rows; it does not take the selection.
    expect(store.activeSession?.session_id).toBe('s2')
  })

  it('does not let a hide during a pending create change the successor', async () => {
    const store = await started()
    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()

    store.hide()
    pending.resolve({ session: sessionPayload('s2') })

    expect(await create).toBe(true)
    // The pane is hidden, so nothing is connected, but the session that was
    // created is still listed for when the pane comes back.
    expect(store.visible).toBe(false)
    expect(store.sessions.map((session) => session.session_id)).toContain('s2')
    expect(fakeSockets).toHaveLength(0)
  })

  it('does not let a dispose during a pending create change anything', async () => {
    const store = await started()
    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()

    store.dispose()
    pending.resolve({ session: sessionPayload('s2') })

    // A disposed store answers nothing: the late reply changes no state.
    expect(await create).toBe(false)
    expect(store.phase).toBe('closed')
    expect(store.visible).toBe(false)
    expect(store.sessions.map((session) => session.session_id)).not.toContain('s2')
  })
})

// ── Who may open a connection ────────────────────────────────────────────
//
// A connection belongs to a visible, unlocked pane with a matching surface.
// Anything else is a state the user has to leave before a socket may open,
// and a session another connection took over does not come back on its own.

describe('terminal store connection eligibility', () => {
  async function started() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    return store
  }

  it('refuses to reconnect while the pane is hidden', async () => {
    const store = await started()
    store.hide()
    const before = fakeSockets.length

    expect(await store.reconnect()).toBe(false)
    expect(fakeSockets).toHaveLength(before)
  })

  it('refuses to reconnect while the pane is locked', async () => {
    const store = await started()
    // The engine says the terminal is not available: that is a state the
    // user has to leave, and nothing may open a socket in it.
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') {
        return Promise.resolve(statusPayload({ available: false, reason: 'https_required' }))
      }
      return Promise.resolve({ sessions: [] })
    })
    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)
    const before = fakeSockets.length

    expect(store.phase).toBe('locked')
    expect(await store.reconnect()).toBe(false)
    expect(fakeSockets).toHaveLength(before)
  })

  it('refuses to reconnect after the store is disposed', async () => {
    const store = await started()
    store.dispose()
    const before = fakeSockets.length

    expect(await store.reconnect()).toBe(false)
    expect(fakeSockets).toHaveLength(before)
  })

  it('does not take a session back after another connection took it over', async () => {
    const store = await started()
    fakeSockets[0].close(4409)
    await vi.advanceTimersByTimeAsync(0)
    expect(store.phase).toBe('takeover')
    const before = fakeSockets.length

    // The page coming back into view does not silently reclaim the session.
    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(0)
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)

    expect(fakeSockets).toHaveLength(before)
    expect(store.phase).toBe('takeover')
  })

  it('closes a connection that never completes the terminal handshake', async () => {
    const store = await started()
    const socket = fakeSockets[0]

    // The transport opens, but the engine never says the session is ready.
    await vi.advanceTimersByTimeAsync(5000)

    expect(socket.closed).toBe(true)
    expect(store.phase).not.toBe('live')
  })

  it('starts the retry budget again only after a completed handshake', async () => {
    const store = await started()
    fakeSockets[0].ready()
    expect(store.phase).toBe('live')

    // A dropped connection after a completed handshake gets the full budget.
    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fakeSockets.length).toBeGreaterThan(1)
  })

  it('keeps the page listeners after a session ends', async () => {
    const store = await started()
    apiDel.mockResolvedValue({ ok: true })
    const removeDocument = vi.spyOn(document, 'removeEventListener')

    await store.endSession()

    // Ending a session is not hiding the pane: the page stays wired up.
    expect(removeDocument).not.toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    expect(store.visible).toBe(true)
  })

  it('wires the page listeners back up when the pane opens again', async () => {
    const store = await started()
    store.hide()
    const addDocument = vi.spyOn(document, 'addEventListener')

    await store.open({ chatId: 'chat-1' })

    expect(addDocument).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
  })
})

// ── Bounded responses ────────────────────────────────────────────────────

describe('terminal store bounded responses', () => {
  it('refuses a session without a folder', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({ sessions: [sessionPayload('s1', { folder: '' })] })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.sessions).toEqual([])
    expect(store.error).toContain('session list')
  })

  it('refuses a list with more sessions than the engine keeps', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({
          sessions: [
            sessionPayload('s1'), sessionPayload('s2'), sessionPayload('s3'),
            sessionPayload('s4'), sessionPayload('s5'),
          ],
        })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.sessions).toEqual([])
    expect(store.error).toContain('session list')
  })

  it('accepts a list of exactly the sessions the engine keeps', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({
          sessions: [
            sessionPayload('s1'), sessionPayload('s2'),
            sessionPayload('s3'), sessionPayload('s4'),
          ],
        })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.sessions).toHaveLength(4)
    expect(store.error).toBe('')
  })
})

// ── Scope, URLs and strict parsing ───────────────────────────────────────

describe('terminal store scope and responses', () => {
  it('scopes the session list to the chat id, encoded', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat/one?x=1' })

    expect(apiGet).toHaveBeenCalledWith('/api/terminal/sessions?chat_id=chat%2Fone%3Fx%3D1')
  })

  it('scopes the session list to the project id instead', async () => {
    const store = useTerminalStore()
    await store.open({ projectId: 'proj-1' })

    expect(apiGet).toHaveBeenCalledWith('/api/terminal/sessions?project_id=proj-1')
  })

  it('never sends a folder, a path or a command', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()

    const body = apiPost.mock.calls.find((call) => call[0] === '/api/terminal/sessions')?.[1]
    expect(body).toEqual({ chat_id: 'chat-1' })
    expect(Object.keys(body as object)).toEqual(['chat_id'])
  })

  it('refuses a session list it does not understand', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({ sessions: [{ session_id: 's1' }] })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.sessions).toEqual([])
    expect(store.error).toContain('did not report a session list')
  })

  it('refuses a status it does not understand', async () => {
    apiGet.mockResolvedValue({ available: 'yes' })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.phase).toBe('error')
    expect(store.status).toBeNull()
  })

  it('pins the session id into one encoded socket path segment', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({ sessions: [sessionPayload('s/../evil?x=1')] })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    store.attachRenderer(makeRenderer('s/../evil?x=1').renderer)

    expect(fakeSockets[0].url).toBe(
      'ws://localhost:3000/ws/terminal/s%2F..%2Fevil%3Fx%3D1',
    )
  })
})

// ── Status, the grant and the session list ────────────────────────────────

describe('terminal store status and grant', () => {
  it('locks the pane when the engine says the terminal needs HTTPS', async () => {
    apiGet.mockResolvedValue(statusPayload({ available: false, reason: 'https_required' }))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.phase).toBe('locked')
    expect(store.error).toContain('secure connection')
    expect(store.unlocked).toBe(false)
  })

  it('asks for no grant when the terminal is unavailable', async () => {
    apiGet.mockResolvedValue(statusPayload({ available: false, reason: 'https_required' }))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(apiPost).not.toHaveBeenCalled()
    expect(store.unlocked).toBe(false)
  })

  it('asks for the grant itself, with an empty body and no password', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    apiGet.mockResolvedValue(statusPayload({ unlock_expires_in: 0 }))
    apiPost.mockResolvedValue({ ok: true, expires_in: 900 })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.unlocked).toBe(true)
    expect(apiPost).toHaveBeenCalledTimes(1)
    // One POST, and it carries no body at all: there is no password to
    // send, because the session is the terminal's trust boundary.
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/unlock')
    const body = apiPost.mock.calls[0][1]
    expect(body).toBeUndefined()
    expect(JSON.stringify(store.$state)).not.toContain('password')
    expect(setItem).not.toHaveBeenCalled()
  })

  it('keeps a live grant instead of re-minting it', async () => {
    // Two tabs of one browser share the cookie: re-minting the grant would
    // revoke the other tab's.
    apiGet.mockResolvedValue(statusPayload())
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.unlocked).toBe(true)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('leaves the pane locked after a refused grant, and creates no shell', async () => {
    apiGet.mockResolvedValue(statusPayload({ unlock_expires_in: 0 }))
    apiPost.mockRejectedValue(apiError(403, 'forbidden origin'))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.phase).toBe('locked')
    expect(store.unlocked).toBe(false)
    expect(store.error).not.toContain('forbidden origin')
    expect(apiPost).toHaveBeenCalledTimes(1)
    // No shell was started: only the grant was refused.
    expect(apiPost).not.toHaveBeenCalledWith('/api/terminal/sessions', expect.anything())
  })

  it('exposes no unlock action and no password state', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect('unlock' in store).toBe(false)
    expect('unlocking' in store).toBe(false)
    expect(JSON.stringify(store.$state)).not.toContain('password')
  })

  it('lists the sessions of the scope and adopts an existing one', async () => {
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({
          sessions: [sessionPayload('s-old', { exited: true }), sessionPayload('s-live')],
        })
      }
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.sessions.map((session) => session.session_id)).toEqual(['s-old', 's-live'])
    expect(store.activeSession?.session_id).toBe('s-live')
    expect(store.phase).toBe('ready')
  })
})

// ── Session lifecycle ────────────────────────────────────────────────────

describe('terminal store session lifecycle', () => {
  it('creates a session only when asked, with exactly one scope id', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })

    const created = await store.createSession()

    expect(created).toBe(true)
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/sessions', { chat_id: 'chat-1' })
    expect(store.activeSession?.session_id).toBe('s1')
  })

  it('refuses a second create while the first is in flight', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)

    const first = store.createSession()
    const second = store.createSession()

    // The second click is refused while the first request is open, so only
    // one shell is ever asked for.
    expect(apiPost).toHaveBeenCalledTimes(1)

    pending.resolve({ session: sessionPayload('s1') })
    expect(await first).toBe(true)
    expect(await second).toBe(false)
  })

  it('does not repeat the create when the outcome is unknown', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockRejectedValue(new Error('network down'))

    const created = await store.createSession()
    await vi.advanceTimersByTimeAsync(30_000)

    expect(created).toBe(false)
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(store.error).toContain('could not be confirmed')
  })

  it('says a refusal is a refusal, not an unknown outcome', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockRejectedValue(apiError(409, 'too many sessions'))

    await store.createSession()

    expect(store.error).toContain('refused')
    expect(store.error).not.toContain('could not be confirmed')
  })

  it('closes the old session socket when a create selects a new session', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    const first = fakeSockets[0]
    first.ready()

    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()
    pending.resolve({ session: sessionPayload('s2') })

    expect(await create).toBe(true)
    // The new session takes the selection, and the old session's socket
    // goes with it: the socket belongs to a session, and the engine allows
    // one controller per session.
    expect(store.activeSession?.session_id).toBe('s2')
    expect(first.closed).toBe(true)
  })

  it('does not let a stale create take the selection from a successor', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    apiPost.mockResolvedValue({ session: sessionPayload('s3') })
    await store.createSession()
    expect(store.activeSession?.session_id).toBe('s3')

    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()
    // The user picks another session while the request is open.
    store.selectSession('s1')
    pending.resolve({ session: sessionPayload('s2') })

    // The shell was created on the engine, so the call succeeded; but the
    // selection belongs to the user, and a late reply does not take it.
    expect(await create).toBe(true)
    expect(store.activeSession?.session_id).toBe('s1')
    expect(store.sessions.map((session) => session.session_id)).toContain('s2')
  })

  it('does not let a late write move the cursor of a replaced renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1', true)
    store.attachRenderer(first.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The surface is replaced and the old write finally resolves: the
    // acknowledged offset must not move for the new surface.
    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    first.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('does not let a failed write corrupt the byte budget of a dropped queue', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    let fail = true
    store.attachRenderer({
      sessionId: 's1',
      reset() {},
      write() {
        return fail
          ? Promise.reject(new Error('write failed'))
          : Promise.resolve()
      },
    })
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not be written')

    // The failed bytes are not counted twice, so the next session of output
    // does not trip the queue bound on a stale total.
    fail = false
    store.error = ''
    store.phase = 'ready'
    socket.receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).not.toContain('faster than the terminal')
  })

  it('does not count a failed write against a queue that was already dropped', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The write never settles until the test says so.
    const pending: { fail: () => void } = { fail: () => {} }
    const view = {
      sessionId: 's1',
      reset() {},
      write() {
        return new Promise<void>((_resolve, reject) => {
          pending.fail = () => reject(new Error('write failed'))
        })
      },
    }
    store.attachRenderer(view)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The queue is dropped while the write is still in flight, so its bytes
    // are already out of the budget. The failed write must not subtract them
    // a second time: the budget would go negative and every later burst
    // would be measured against a wrong total. Nor may it fail the store:
    // the surface it belonged to is gone.
    store.hide()
    await vi.advanceTimersByTimeAsync(0)
    pending.fail()
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).not.toContain('could not be written')

    // The budget is still whole: more than a megabyte of output must trip
    // the bound. A budget that went short by the failed chunk would let the
    // last chunk through.
    store.visible = true
    expect(await store.reconnect()).toBe(true)
    const fresh = fakeSockets[fakeSockets.length - 1]
    const chunk = new Uint8Array(64 * 1024)
    for (let i = 0; i < 16; i += 1) {
      fresh.receive({
        type: 'output', start: i * chunk.length, end: (i + 1) * chunk.length,
        data: encodeTerminalBytes(chunk),
      })
    }
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).not.toContain('faster than the terminal')
    expect(fakeSockets[fakeSockets.length - 1].sent.length).toBeGreaterThan(0)

    // The next chunk does not fit any more.
    fresh.receive({
      type: 'output', start: 16 * chunk.length, end: 17 * chunk.length,
      data: encodeTerminalBytes(chunk),
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(store.error).toContain('faster than the terminal')
  })

  it('does not let a pending retry open a second connection over a new one', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    // The socket drops and the retry waits for the status.
    const status = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return status.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    expect(fakeSockets).toHaveLength(1)
    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)
    // A fresh connection is opened in the meantime.
    await store.reconnect()
    expect(fakeSockets).toHaveLength(2)
    // The engine answers the new connection, so it stays open.
    fakeSockets[1].ready()

    // The pending retry belongs to the connection it was scheduled for, so
    // it must not even schedule a second one over the live connection.
    status.resolve(statusPayload())
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fakeSockets).toHaveLength(2)
    // The live connection is the one that is still open.
    expect(fakeSockets[1].closed).toBe(false)
  })

  it('does not let a late write move the cursor of a reset stream', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The stream is reset while the write is still in flight, so the bytes
    // belong to a stream that no longer exists.
    store.selectSession('s1')
    store.hide()
    await vi.advanceTimersByTimeAsync(0)

    // The late write resolves now: the cursor of the reset stream must not
    // move for bytes that belonged to the old one.
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('does not let a stale list pull the selection back to an older session', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    apiPost.mockResolvedValue({ session: sessionPayload('s2') })
    await store.createSession()
    expect(store.activeSession?.session_id).toBe('s2')

    // A refresh that was already in flight when the user picked s1. Its
    // answer arrives after the pick and must not move the selection back to
    // the session the list would have chosen on its own.
    const pending = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return pending.promise
      return Promise.resolve({})
    })
    const refresh = store.refreshSessions()
    store.selectSession('s1')
    pending.resolve({ sessions: [sessionPayload('s2')] })
    await refresh

    expect(store.activeSession?.session_id).toBe('s1')
  })

  it('does not adopt a session from a list that arrived after the user picked one', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    apiPost.mockResolvedValue({ session: sessionPayload('s2') })
    await store.createSession()
    // The selection is dropped, so a list would choose one on its own.
    store.selectSession('s2')
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    expect(store.activeSession?.session_id).toBe('s1')

    const pending = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) return pending.promise
      return Promise.resolve({})
    })
    // The list is in flight with no selection to speak of: the user picks a
    // session while it is open.
    const refresh = store.refreshSessions()
    store.selectSession('s2')
    pending.resolve({ sessions: [sessionPayload('s-other')] })
    await refresh

    expect(store.activeSession?.session_id).toBe('s2')
  })

  it('does not let a stale create yank the selection back', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    apiPost.mockResolvedValue({ session: sessionPayload('s2') })
    await store.createSession()

    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()
    store.selectSession('s1')
    pending.resolve({ session: sessionPayload('s3') })
    expect(await create).toBe(true)

    expect(store.activeSession?.session_id).toBe('s1')
  })

  it('finds a session that appeared elsewhere when the list is refreshed', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockRejectedValue(new Error('network down'))
    await store.createSession()

    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      if (path.startsWith('/api/terminal/sessions')) {
        return Promise.resolve({ sessions: [sessionPayload('s-made-elsewhere')] })
      }
      return Promise.resolve({})
    })
    await store.refreshSessions()

    expect(store.sessions.map((session) => session.session_id)).toEqual(['s-made-elsewhere'])
  })

  it('ends a session with an encoded id', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('a/b') })
    await store.createSession()
    apiDel.mockResolvedValue({ ok: true })

    const ended = await store.endSession()

    expect(ended).toBe(true)
    expect(apiDel).toHaveBeenCalledWith('/api/terminal/sessions/a%2Fb')
    expect(store.sessions).toEqual([])
  })

  it('does not end the session when the terminal is hidden', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()

    store.hide()

    expect(apiDel).not.toHaveBeenCalled()
    expect(store.visible).toBe(false)
  })
})

// ── The attach offset ────────────────────────────────────────────────────

describe('terminal store attach offset', () => {
  it('attaches with offset zero on a first renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    const attach = fakeSockets[0].ofType('attach')
    expect(attach).toHaveLength(1)
    expect(attach[0]).toEqual({ type: 'attach', offset: 0, takeover: false })
  })

  it('resumes from the acknowledged cursor, never from ready.seq', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)

    const socket = fakeSockets[0]
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 0, truncated: false,
    })
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    await store.reconnect()

    const attach = fakeSockets[1].ofType('attach')
    expect(attach[0]).toEqual({ type: 'attach', offset: 5, takeover: false })
  })

  it('resumes from the acknowledged cursor when the same surface comes back', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The surface goes away and comes back: it still holds the five bytes,
    // so it resumes from them rather than taking the replay again.
    store.detachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)
    store.attachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)

    const attach = fakeSockets[fakeSockets.length - 1].ofType('attach')
    expect(attach[0]).toEqual({ type: 'attach', offset: 5, takeover: false })
  })

  it('takes the whole replay for a surface the store has never seen', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1')
    store.attachRenderer(first.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // A different surface has an empty screen, so it takes the replay from
    // the start.
    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    await vi.advanceTimersByTimeAsync(0)

    const attach = fakeSockets[fakeSockets.length - 1].ofType('attach')
    expect(attach[0]).toEqual({ type: 'attach', offset: 0, takeover: false })
  })

  it('refuses a surface that belongs to another session', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()

    store.attachRenderer(makeRenderer('s-other').renderer)

    expect(store.error).toContain('do not match')
    expect(fakeSockets).toHaveLength(0)
  })

  it('does not let replay_end advance the cursor', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    const socket = fakeSockets[0]
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 0, truncated: false,
    })
    socket.receive({ type: 'replay_end', seq: 4096, truncated: false })
    await vi.advanceTimersByTimeAsync(0)

    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('does not advance the cursor before the renderer acknowledges the write', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)

    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })

    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[2].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
  })
})

// ── Output, duplicates, gaps and truncation ──────────────────────────────

describe('terminal store output handling', () => {
  async function attached() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    return { store, view, socket: fakeSockets[0] }
  }

  it('writes a partial duplicate once, from the cursor on', async () => {
    const { view, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    // The same six bytes, of which the first two are already on the screen.
    socket.receive({ type: 'output', start: 3, end: 9, data: b64('loorld') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(2)
    expect(new TextDecoder().decode(view.writes[0])).toBe('hello')
    expect(new TextDecoder().decode(view.writes[1])).toBe('orld')
  })

  it('ignores a full duplicate', async () => {
    const { view, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(1)
  })

  it('stops on a gap rather than inventing bytes', async () => {
    const { store, view, socket } = await attached()
    socket.receive({ type: 'output', start: 100, end: 105, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(0)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('never received')
  })

  it('stops on a gap that opens after a good frame', async () => {
    const { store, view, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    // The next frame starts past the bytes this client received.
    socket.receive({ type: 'output', start: 500, end: 505, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(1)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('never received')
  })

  it('stops on a gap the screen is already past', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer never acknowledges, so the screen is ahead of what this
    // client received: a frame that starts past the received bytes is still
    // a gap, however much the screen holds.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({ type: 'output', start: 500, end: 505, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(1)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('never received')
  })

  it('acknowledges nothing for a duplicate the renderer already confirmed', async () => {
    const { store, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The same bytes again: nothing new for the screen, so nothing is
    // written — and nothing is acknowledged either. The acknowledgement
    // belongs to the renderer, and the renderer already gave it.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
  })

  it('does not acknowledge a duplicate the renderer has not confirmed', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer never acknowledges, so the screen is at five and the
    // acknowledged offset is still at zero.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The engine replays the same five bytes. The screen holds them, so
    // nothing is written a second time — but a duplicate is not a
    // confirmation: the renderer never said it took them, so the resume
    // point stays where the renderer left it.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(view.writes).toHaveLength(1)
    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('resets the renderer and warns when the retained stream was truncated', async () => {
    const { store, view, socket } = await attached()
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)

    // The screen is cleared before the replay is written over it, and the
    // clear happens with the first write of the replay.
    socket.receive({ type: 'output', start: 50, end: 55, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(1)
    expect(store.truncated).toBe(true)
    expect(store.warning).toContain('truncated')
    expect(new TextDecoder().decode(view.writes[0])).toBe('world')
  })

  it('clears the screen before the replay when a write is still in flight', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer never confirms, so a write is still in flight when the
    // truncated replay arrives.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)

    // The clear waits for the write in flight: a reset now would put the
    // bytes that are still coming back onto the cleared screen.
    expect(view.resets).toBe(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(view.resets).toBe(1)
  })

  it('acknowledges the replayed bytes once the truncated screen is cleared', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]

    // The engine dropped the bytes before the retained buffer and says so.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({ type: 'output', start: 50, end: 55, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(1)
    expect(new TextDecoder().decode(view.writes[0])).toBe('world')

    // The replay was rendered and confirmed, so the resume point is its end.
    await store.reconnect()
    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 55, takeover: false,
    })
  })

  it('does not acknowledge bytes the screen does not hold', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The surface is replaced while the write is in flight: the bytes are
    // going to a screen that is no longer the one on show.
    const successor = makeRenderer('s1')
    store.attachRenderer(successor.renderer)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)

    // The old surface's write confirms bytes the new surface does not hold,
    // so the resume point is the new surface's own acknowledgement: zero.
    await store.reconnect()
    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('does not acknowledge a write for a surface that was replaced', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The surface is replaced while the write is in flight, and the write
    // resolves after the replacement.
    const successor = makeRenderer('s1')
    store.attachRenderer(successor.renderer)
    await vi.advanceTimersByTimeAsync(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)

    // The successor never took those bytes, so its own acknowledgement is
    // still zero and the resume point is zero.
    expect(successor.writes).toHaveLength(0)
    await store.reconnect()
    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('drops the bytes queued for the stream a truncated replay replaced', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The first write is never confirmed, so the second one stays in the
    // queue when the truncation arrives.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({ type: 'output', start: 5, end: 10, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)
    expect(view.writes).toHaveLength(1)

    // The engine says it dropped the bytes before the retained buffer.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)

    // The bytes of the dropped stream that were still waiting never reach
    // the screen: the engine is about to send the replay, and the screen
    // was cleared for it.
    expect(view.resets).toBe(1)
    expect(view.writes).toHaveLength(1)
  })

  it('does not confirm a write for a stream the surface no longer shows', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The engine replaces the stream: the retained buffer starts after the
    // bytes this surface was given, and says it dropped the rest.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)

    // The write of the dropped stream resolves now. It confirms bytes the
    // surface is not showing, so the resume point is the replay's base.
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[fakeSockets.length - 1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 50, takeover: false,
    })
  })

  it('clears a detached surface only after its own write has finished', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The engine replaces the stream, so the surface owes a clear.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(view.resets).toBe(0)

    // The surface is detached before the clear could be applied. The clear
    // belongs to that surface and waits for its own write: clearing now
    // would let the write that is in flight land on the cleared screen.
    store.detachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)
    expect(view.resets).toBe(0)

    // The write finishes, and the clear follows it.
    view.settle()
    await vi.advanceTimersByTimeAsync(0)
    expect(view.resets).toBe(1)
  })

  it('clears the dropped stream off the screen when a truncated replay replaces it', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer never confirms, so the first write stays in flight and
    // the bytes of the old stream are still queued behind it.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The engine says it dropped the bytes before the retained buffer.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)
    // The replay follows.
    socket.receive({ type: 'output', start: 50, end: 55, data: b64('world') })
    await vi.advanceTimersByTimeAsync(0)
    view.settle()
    await vi.advanceTimersByTimeAsync(0)

    // The screen was cleared after the stale write landed, so the bytes of
    // the dropped stream are not on it any more: the clear happened after
    // that write and before the replay.
    expect(view.resets).toBe(1)
    const order = view.writes.map((bytes) => new TextDecoder().decode(bytes))
    expect(order[order.length - 1]).toBe('world')
  })

  it('does not write the same bytes twice when the terminal comes back', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]

    // The renderer takes the bytes but never acknowledges them.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    expect(view.writes).toHaveLength(1)

    // Hiding drops what was never acknowledged, so the engine replays it.
    store.hide()
    await vi.advanceTimersByTimeAsync(0)
    store.attachRenderer(view.renderer)
    await vi.advanceTimersByTimeAsync(0)

    const attach = fakeSockets[fakeSockets.length - 1].ofType('attach')
    expect(attach[0]).toEqual({ type: 'attach', offset: 0, takeover: false })

    // The engine replays the same five bytes. The screen already holds them,
    // so they are trimmed rather than written a second time — a terminal
    // that shows every line twice is a terminal nobody trusts.
    const replayed = fakeSockets[fakeSockets.length - 1]
    replayed.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.writes).toHaveLength(1)
  })

  it('refuses to continue when the engine dropped bytes without saying so', async () => {
    const { store, view, socket } = await attached()
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: false,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(0)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('skipped output')
  })

  it('refuses a gap the engine admits to, measured against the acknowledged offset', async () => {
    const { store, view, socket } = await attached()
    // The renderer acknowledged five bytes; the engine's buffer starts at 50
    // and says nothing was lost, so bytes 5..50 are gone.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: false,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(0)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('skipped output')
  })

  it('refuses a gap the engine admits to even when the screen is further ahead', async () => {
    const { store, view, socket } = await attached()
    // The screen holds five bytes and the renderer acknowledged them, so
    // both cursors are at five. The engine's buffer starts at fifty: the
    // bytes in between are gone, whatever the screen holds.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: false,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(0)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('skipped output')
  })

  it('refuses a gap the engine admits to when the screen is ahead of the acknowledgement', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer takes the bytes but never acknowledges them, so the
    // screen is at five and the acknowledged offset is still at zero.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    // The engine's buffer starts at three: past the acknowledged offset, so
    // the bytes in between are gone even though the screen shows them.
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 3, truncated: false,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(0)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('skipped output')
  })

  it('writes arbitrary bytes without re-encoding them', async () => {
    const { view, socket } = await attached()
    const raw = new Uint8Array([0x00, 0xc3, 0x28, 0xff, 0xfe, 0x1b])
    socket.receive({
      type: 'output', start: 0, end: raw.length, data: encodeTerminalBytes(raw),
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(Array.from(view.writes[0])).toEqual(Array.from(raw))
  })

  it('ends the phase when the shell exits, and keeps the transcript', async () => {
    const { store, view, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    socket.receive({ type: 'exit', code: 0 })
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('ended')
    expect(view.writes).toHaveLength(1)
  })
})

// ── Stale work ───────────────────────────────────────────────────────────

describe('terminal store stale work', () => {
  it('does not let a stale status reply land on a new target', async () => {
    const first = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return first.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const store = useTerminalStore()
    const staleOpen = store.open({ chatId: 'chat-1' })

    const second = deferred<unknown>()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return second.promise
      if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions: [] })
      return Promise.resolve({})
    })
    const freshOpen = store.open({ chatId: 'chat-2' })
    second.resolve(statusPayload())
    await freshOpen
    first.resolve(statusPayload({ host: 'stale-host' }))
    await staleOpen

    expect(store.target).toEqual({ chatId: 'chat-2' })
    expect(store.status?.host).toBe('engine.local')
  })

  it('does not let a stale socket close clobber the new socket', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    const staleClose = fakeSockets[0].onclose
    await store.reconnect()
    staleClose?.({ code: 4401 } as CloseEvent)
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).not.toBe('locked')
    expect(store.unlocked).toBe(true)
  })

  it('does not let a stale frame write to a replaced renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1')
    store.attachRenderer(first.renderer)
    const staleMessage = fakeSockets[0].onmessage

    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    staleMessage?.({ data: JSON.stringify({ type: 'output', start: 0, end: 5, data: b64('old') }) })
    await vi.advanceTimersByTimeAsync(0)

    expect(first.writes).toHaveLength(0)
    expect(second.writes).toHaveLength(0)
  })

  it('does not let a late write advance the cursor of the next renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1', true)
    store.attachRenderer(first.renderer)
    fakeSockets[0].receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    first.settle()
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: false,
    })
  })

  it('does not let a stale detach close the successor renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const first = makeRenderer('s1')
    store.attachRenderer(first.renderer)
    const second = makeRenderer('s1')
    store.attachRenderer(second.renderer)
    const successor = fakeSockets[fakeSockets.length - 1]

    store.detachRenderer(first.renderer)
    await vi.advanceTimersByTimeAsync(0)

    expect(successor.closed).toBe(false)
  })
})

// ── Bounds ───────────────────────────────────────────────────────────────

describe('terminal store bounds', () => {
  async function connected() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1')
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    // The terminal is live only once the engine has said the session is
    // ready: input before that would go to a session that is not there yet.
    socket.ready()
    return { store, view, socket }
  }

  it('refuses input larger than the frame the engine accepts', async () => {
    const { store, socket } = await connected()

    const sent = store.sendInput(new Uint8Array(65537))

    expect(sent).toBe(false)
    expect(store.error).toContain('too large')
    expect(socket.ofType('input')).toHaveLength(0)
  })

  it('refuses input while the socket buffer is full', async () => {
    const { store, socket } = await connected()
    socket.bufferedAmount = 256 * 1024

    const sent = store.sendInput(new Uint8Array([0x41]))

    expect(sent).toBe(false)
    expect(store.error).toContain('not keeping up')
    expect(socket.ofType('input')).toHaveLength(0)
  })

  it('sends input as base64 bytes', async () => {
    const { store, socket } = await connected()

    expect(store.sendInput(new Uint8Array([0x41]))).toBe(true)
    expect(socket.ofType('input')[0]).toEqual({ type: 'input', data: 'QQ==' })
  })

  it('refuses input before the terminal handshake completed', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    const socket = fakeSockets[0]

    // The transport is open, but the engine has not said the session is
    // ready, so there is nothing to type into yet.
    expect(store.sendInput(new Uint8Array([0x41]))).toBe(false)
    expect(store.error).toContain('not connected')
    expect(socket.ofType('input')).toHaveLength(0)
  })

  it('refuses a malformed frame without acting on it', async () => {
    const { store, socket } = await connected()

    socket.receive({ type: 'output', start: 0, end: 5 })
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not read')
  })

  it('refuses a frame whose byte interval does not match its payload', async () => {
    const { store, socket } = await connected()

    socket.receive({ type: 'output', start: 0, end: 10, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not read')
  })

  it('refuses a frame whose type is an inherited name', async () => {
    const { store, socket } = await connected()

    socket.receive('{"type":"constructor","session_id":"s1"}')
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not read')
  })

  it('clamps a resize to the bounds the engine accepts', async () => {
    const { store, socket } = await connected()

    store.resize(5, 1)
    store.resize(1000, 1000)

    expect(socket.ofType('resize')).toEqual([
      { type: 'resize', cols: 20, rows: 5 },
      { type: 'resize', cols: 500, rows: 200 },
    ])
  })

  it('refuses a resize that is not a whole number of cells', async () => {
    const { store, socket } = await connected()

    expect(store.resize(80.5, 24)).toBe(false)
    expect(socket.ofType('resize')).toHaveLength(0)
  })

  it('closes the socket when the output queue overflows', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]

    // A megabyte of pending output with a renderer that never acknowledges.
    const chunk = new Uint8Array(64 * 1024)
    for (let i = 0; i < 17; i += 1) {
      socket.receive({
        type: 'output', start: i * chunk.length, end: (i + 1) * chunk.length,
        data: encodeTerminalBytes(chunk),
      })
    }
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('faster than the terminal')
    expect(view.writes.length).toBeLessThan(17)

    // The stream is broken, so it is not retried: a new connection would
    // replay the same output into the same slow screen.
    await vi.advanceTimersByTimeAsync(60_000)
    expect(fakeSockets).toHaveLength(1)
    expect(store.phase).toBe('error')
  })
})

// ── Close codes and takeover ─────────────────────────────────────────────

describe('terminal store close codes', () => {
  async function connected() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    return { store, socket: fakeSockets[0] }
  }

  it('asks for a new grant when the engine closed the stream', async () => {
    const { store, socket } = await connected()
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') {
        return Promise.resolve(statusPayload({ unlock_expires_in: 0 }))
      }
      return Promise.resolve({ sessions: [] })
    })
    apiPost.mockResolvedValue({ ok: true, expires_in: 900 })
    const before = fakeSockets.length

    socket.close(4401)

    expect(store.unlocked).toBe(false)

    // The pane asks the engine for a new grant by itself: the signed-in
    // session is the terminal's trust boundary, and there is no password
    // to ask for.
    await vi.advanceTimersByTimeAsync(0)
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/unlock')
    expect(store.unlocked).toBe(true)
    expect(store.error).toBe('')

    // And the stream is resumed on the backoff: a fresh grant, a fresh
    // socket, and the session attached again.
    await vi.advanceTimersByTimeAsync(250)
    expect(fakeSockets).toHaveLength(before + 1)
    const resumed = fakeSockets[fakeSockets.length - 1]
    expect(resumed.ofType('attach')).toHaveLength(1)
    expect(resumed.ofType('attach')[0].takeover).toBe(false)
  })

  it('keeps asking for a grant the engine will not give', async () => {
    const { store, socket } = await connected()
    // The engine is unreachable: the status read fails and the grant is
    // refused. There is no form for the user to fill in any more, so the
    // pane has to keep trying by itself.
    apiGet.mockRejectedValue(new Error('network down'))
    apiPost.mockRejectedValue(new Error('network down'))
    const unlocks = () =>
      apiPost.mock.calls.filter((call) => call[0] === '/api/terminal/unlock').length

    socket.close(4401)
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).toBe('locked')
    expect(unlocks()).toBe(1)

    // The next attempt asks again, on the backoff.
    await vi.advanceTimersByTimeAsync(250)
    expect(unlocks()).toBe(2)

    // And when the engine answers again, the pane reconnects.
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') return Promise.resolve(statusPayload())
      return Promise.resolve({ sessions: [] })
    })
    apiPost.mockResolvedValue({ ok: true, expires_in: 900 })
    const before = fakeSockets.length

    await vi.advanceTimersByTimeAsync(5000)
    expect(fakeSockets.length).toBeGreaterThan(before)
    expect(store.unlocked).toBe(true)
  })

  it('stops asking for a grant once the attempt budget runs out', async () => {
    const { store, socket } = await connected()
    apiGet.mockRejectedValue(new Error('network down'))
    apiPost.mockRejectedValue(new Error('network down'))
    const unlocks = () =>
      apiPost.mock.calls.filter((call) => call[0] === '/api/terminal/unlock').length

    socket.close(4401)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(60_000)

    // The budget is finite: the pane stops asking and says so, rather
    // than asking forever.
    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not be re-established')
    const asked = unlocks()
    await vi.advanceTimersByTimeAsync(60_000)
    expect(unlocks()).toBe(asked)
  })

  it('recovers on the older auth close code too', async () => {
    const { store, socket } = await connected()

    socket.close(4001)

    expect(store.phase).toBe('reconnecting')
  })

  it('reports a refused origin as an error', async () => {
    const { store, socket } = await connected()

    socket.close(4003)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('origin')
  })

  it('reports a gone session as ended', async () => {
    const { store, socket } = await connected()

    socket.close(4404)

    expect(store.phase).toBe('ended')
  })

  it('reports a slow consumer as an error', async () => {
    const { store, socket } = await connected()

    socket.close(4413)

    expect(store.phase).toBe('error')
    expect(store.error).toContain('too slow')
  })

  it('takes over only when the takeover is explicit', async () => {
    const { store, socket } = await connected()
    socket.close(4409)

    expect(store.phase).toBe('takeover')
    expect(await store.reconnect()).toBe(false)
    expect(fakeSockets).toHaveLength(1)

    expect(await store.reconnect(true)).toBe(true)
    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 0, takeover: true,
    })
  })

  it('never creates a session on a reconnect', async () => {
    const { store, socket } = await connected()
    const createsBefore = apiPost.mock.calls.filter(
      (call) => call[0] === '/api/terminal/sessions',
    ).length

    socket.close(4409)
    await store.reconnect(true)
    await vi.advanceTimersByTimeAsync(5000)

    const createsAfter = apiPost.mock.calls.filter(
      (call) => call[0] === '/api/terminal/sessions',
    ).length
    expect(createsAfter).toBe(createsBefore)
  })
})

// ── Retries ──────────────────────────────────────────────────────────────

describe('terminal store retries', () => {
  it('reads the status before retrying an abnormal closure', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    apiGet.mockClear()

    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)

    await vi.advanceTimersByTimeAsync(250)
    expect(fakeSockets).toHaveLength(2)
    // The grant was still live, so the reconnect asked for nothing: a
    // retry resumes the session, it does not re-mint the grant.
    expect(
      apiPost.mock.calls.filter((call) => call[0] === '/api/terminal/unlock'),
    ).toHaveLength(0)
  })

  it('locks the pane when the status says the terminal is unavailable', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') {
        return Promise.resolve(statusPayload({ available: false, reason: 'https_required' }))
      }
      return Promise.resolve({ sessions: [] })
    })

    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5000)

    expect(store.phase).toBe('locked')
    expect(store.unlocked).toBe(false)
    expect(fakeSockets).toHaveLength(1)
  })

  it('asks for a new grant when the status says the grant is gone', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') {
        return Promise.resolve(statusPayload({ unlock_expires_in: 0 }))
      }
      return Promise.resolve({ sessions: [] })
    })
    apiPost.mockImplementation((path: string) => {
      if (path === '/api/terminal/unlock') return Promise.resolve({ ok: true, expires_in: 900 })
      return Promise.resolve({ session: sessionPayload('s1') })
    })

    fakeSockets[0].close(1006)
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5000)

    // A missing grant is not a dead end: the pane asks for one and resumes.
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/unlock')
    expect(store.unlocked).toBe(true)
    expect(store.phase).not.toBe('locked')
    expect(fakeSockets.length).toBeGreaterThan(1)
  })

  it('gives up after eight automatic attempts', async () => {
    FakeWebSocket.autoOpen = false
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    for (let i = 0; i < 12; i += 1) {
      const last = fakeSockets[fakeSockets.length - 1]
      last.close(1006)
      await vi.advanceTimersByTimeAsync(0)
      await vi.advanceTimersByTimeAsync(5000)
    }

    expect(fakeSockets).toHaveLength(9)
    expect(store.phase).toBe('error')
    expect(store.error).toContain('could not be re-established')
  })
})

// ── Page lifecycle ───────────────────────────────────────────────────────

describe('terminal store page lifecycle', () => {
  async function connected() {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    const socket = fakeSockets[0]
    // The engine says the session is ready: only then is the terminal live
    // and only then does the client keep it alive with a ping.
    socket.ready()
    return { store, socket }
  }

  it('replaces the connection when the page becomes visible again', async () => {
    await connected()

    Object.defineProperty(document, 'visibilityState', {
      value: 'hidden', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)
    expect(fakeSockets).toHaveLength(1)

    Object.defineProperty(document, 'visibilityState', {
      value: 'visible', configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)

    expect(fakeSockets).toHaveLength(2)
  })

  it('replaces the connection when the page is restored from the back/forward cache', async () => {
    await connected()

    window.dispatchEvent(new Event('pageshow'))
    await vi.advanceTimersByTimeAsync(0)

    expect(fakeSockets).toHaveLength(2)
  })

  it('pings a live connection and replaces a stalled one', async () => {
    const { socket } = await connected()

    await vi.advanceTimersByTimeAsync(15_000)
    expect(socket.ofType('ping')).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(15_000)
    expect(fakeSockets).toHaveLength(2)
  })

  it('closes the socket and stops the timers when the terminal is hidden', async () => {
    const { store, socket } = await connected()

    store.hide()
    await vi.advanceTimersByTimeAsync(60_000)

    expect(socket.closed).toBe(true)
    expect(fakeSockets).toHaveLength(1)
  })

  it('stops the listeners, the timers and the socket when the store is disposed', async () => {
    const { store, socket } = await connected()
    const removeWindow = vi.spyOn(window, 'removeEventListener')
    const removeDocument = vi.spyOn(document, 'removeEventListener')

    store.dispose()
    await vi.advanceTimersByTimeAsync(60_000)
    window.dispatchEvent(new Event('pageshow'))
    document.dispatchEvent(new Event('visibilitychange'))
    await vi.advanceTimersByTimeAsync(0)

    expect(socket.closed).toBe(true)
    expect(fakeSockets).toHaveLength(1)
    expect(removeDocument).toHaveBeenCalledWith('visibilitychange', expect.any(Function))
    expect(removeWindow).toHaveBeenCalledWith('pageshow', expect.any(Function))
    expect(store.phase).toBe('closed')
  })

  it('installs no window listener when the module is imported', async () => {
    vi.resetModules()
    const addWindow = vi.spyOn(window, 'addEventListener')
    const addDocument = vi.spyOn(document, 'addEventListener')

    await import('../terminal')

    const terminalListeners = [
      ...addWindow.mock.calls.map((call) => call[0]),
      ...addDocument.mock.calls.map((call) => call[0]),
    ].filter((type) => type === 'visibilitychange' || type === 'pageshow')
    expect(terminalListeners).toEqual([])
  })
})
