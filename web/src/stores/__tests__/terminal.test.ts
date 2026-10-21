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

// ── Status, unlock and the grant ─────────────────────────────────────────

describe('terminal store status and unlock', () => {
  it('locks the pane when the engine says the terminal needs HTTPS', async () => {
    apiGet.mockResolvedValue(statusPayload({ available: false, reason: 'https_required' }))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    expect(store.phase).toBe('locked')
    expect(store.error).toContain('secure connection')
    expect(store.unlocked).toBe(false)
  })

  it('does not send the password when the terminal is unavailable', async () => {
    apiGet.mockResolvedValue(statusPayload({ available: false, reason: 'https_required' }))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    const unlocked = await store.unlock('hunter2')

    expect(unlocked).toBe(false)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('passes the password to one unlock request and keeps it nowhere', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    apiGet.mockResolvedValue(statusPayload({ unlock_expires_in: 0 }))
    apiPost.mockResolvedValue({ ok: true, expires_in: 900 })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    const unlocked = await store.unlock('hunter2')

    expect(unlocked).toBe(true)
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/unlock', { password: 'hunter2' })
    expect(JSON.stringify(store.$state)).not.toContain('hunter2')
    expect(setItem).not.toHaveBeenCalled()
  })

  it('leaves the pane locked after a refused password, and creates no shell', async () => {
    apiGet.mockResolvedValue(statusPayload({ unlock_expires_in: 0 }))
    apiPost.mockRejectedValue(apiError(403, 'bad password'))
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })

    const unlocked = await store.unlock('wrong')

    expect(unlocked).toBe(false)
    expect(store.phase).toBe('locked')
    expect(store.error).not.toContain('wrong')
    expect(apiPost).toHaveBeenCalledTimes(1)
  })

  it('replaces the socket after a fresh unlock, so the old one cannot clobber it', async () => {
    apiGet.mockResolvedValue(statusPayload({ unlock_expires_in: 0 }))
    apiPost.mockImplementation((path: string) => {
      if (path === '/api/terminal/unlock') return Promise.resolve({ ok: true, expires_in: 900 })
      return Promise.resolve({ session: sessionPayload('s1') })
    })
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    await store.unlock('hunter2')
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    const first = fakeSockets[0]
    expect(first).toBeDefined()

    // A fresh unlock changes the grant in the cookie.
    await store.unlock('hunter2')

    expect(first.closed).toBe(true)
    expect(fakeSockets).toHaveLength(2)

    // The old socket's late close must not decide what the new one does.
    first.onclose?.({ code: 4401 } as CloseEvent)
    await vi.advanceTimersByTimeAsync(0)

    expect(store.phase).not.toBe('locked')
    expect(store.unlocked).toBe(true)
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

  it('does not let a stale create land on a replaced renderer', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)

    const pending = deferred<unknown>()
    apiPost.mockReturnValue(pending.promise)
    const create = store.createSession()
    // The surface is replaced while the request is open.
    store.attachRenderer(makeRenderer('s1').renderer)
    pending.resolve({ session: sessionPayload('s2') })

    expect(await create).toBe(false)
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
    // would be measured against a wrong total.
    store.hide()
    await vi.advanceTimersByTimeAsync(0)
    pending.fail()
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).toContain('could not be written')

    // The budget is still whole: exactly a megabyte of output must trip the
    // bound, and not one byte more. A budget that went short by the failed
    // chunk would let the last chunk through.
    store.visible = true
    expect(await store.reconnect()).toBe(true)
    const fresh = fakeSockets[fakeSockets.length - 1]
    const chunk = new Uint8Array(64 * 1024)
    for (let i = 0; i < 15; i += 1) {
      fresh.receive({
        type: 'output', start: i * chunk.length, end: (i + 1) * chunk.length,
        data: encodeTerminalBytes(chunk),
      })
    }
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).not.toContain('faster than the terminal')
    expect(fakeSockets[fakeSockets.length - 1].sent.length).toBeGreaterThan(0)

    // The last chunk that still fits: a megabyte minus what is queued.
    const last = new Uint8Array(1024 * 1024 - 15 * chunk.length)
    fresh.receive({
      type: 'output', start: 15 * chunk.length, end: 15 * chunk.length + last.length,
      data: encodeTerminalBytes(last),
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(store.error).not.toContain('faster than the terminal')

    // One byte more tips it over.
    fresh.receive({
      type: 'output', start: 15 * chunk.length + last.length,
      end: 15 * chunk.length + last.length + 1,
      data: encodeTerminalBytes(new Uint8Array(1)),
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

  it('catches the acknowledgement up when the screen already holds the bytes', async () => {
    const { store, socket } = await attached()
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The same bytes again: nothing new for the screen, so the
    // acknowledgement catches up and a reconnect resumes from there.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
  })

  it('catches the acknowledgement up for a replay the screen already holds', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    // The renderer never acknowledges, so the screen is at five and the
    // acknowledged offset is at zero.
    const view = makeRenderer('s1', true)
    store.attachRenderer(view.renderer)
    const socket = fakeSockets[0]
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)

    // The engine replays the same five bytes. The screen holds them, so
    // nothing is written a second time — but the acknowledgement catches up,
    // because the screen really does show them.
    socket.receive({ type: 'output', start: 0, end: 5, data: b64('hello') })
    await vi.advanceTimersByTimeAsync(0)
    await store.reconnect()

    expect(view.writes).toHaveLength(1)
    expect(fakeSockets[1].ofType('attach')[0]).toEqual({
      type: 'attach', offset: 5, takeover: false,
    })
  })

  it('resets the renderer and warns when the retained stream was truncated', async () => {
    const { store, view, socket } = await attached()
    socket.receive({
      type: 'ready', session_id: 's1', folder: '/tmp', cols: 80, rows: 24,
      seq: 4096, replay_start: 50, truncated: true,
    })
    await vi.advanceTimersByTimeAsync(0)

    expect(view.resets).toBe(1)
    expect(store.truncated).toBe(true)
    expect(store.warning).toContain('truncated')
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
    return { store, view, socket: fakeSockets[0] }
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

  it('locks the pane when the grant was revoked', async () => {
    const { store, socket } = await connected()

    socket.close(4401)

    expect(store.phase).toBe('locked')
    expect(store.unlocked).toBe(false)
  })

  it('locks the pane on the older auth close code too', async () => {
    const { store, socket } = await connected()

    socket.close(4001)

    expect(store.phase).toBe('locked')
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
  })

  it('locks the pane when the status says the grant is gone', async () => {
    const store = useTerminalStore()
    await store.open({ chatId: 'chat-1' })
    apiPost.mockResolvedValue({ session: sessionPayload('s1') })
    await store.createSession()
    store.attachRenderer(makeRenderer('s1').renderer)
    apiGet.mockImplementation((path: string) => {
      if (path === '/api/terminal/status') {
        return Promise.resolve(statusPayload({ available: false, reason: 'unlocked_expired' }))
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
    return { store, socket: fakeSockets[0] }
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
