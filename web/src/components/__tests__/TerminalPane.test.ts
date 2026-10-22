// @vitest-environment jsdom

/**
 * `components/TerminalPane.vue` — the browser terminal's pane.
 *
 * The pane is the part a person touches, so this file is about the parts a
 * person can get wrong: the pane never asks for a password, it obtains the
 * grant itself and lists the shells, opening one gives the session a screen
 * and a connection, Hide detaches without stopping the shell, End stops it,
 * and a takeover refusal is offered as something to do rather than a dead
 * end.
 *
 * The keyboard is the other half. The shell keeps Escape, Tab, the arrows,
 * Ctrl+C and Ctrl+D while it has the focus, and the app's own shortcuts must
 * not fire from inside the terminal — a workspace switch while someone is
 * typing a path is the bug this guards.
 *
 * Everything here is driven by fakes: a fake API, a fake WebSocket and a
 * fake screen. jsdom has no layout and no canvas, so the real xterm cannot
 * run here; the browser spec proves that half.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import TerminalPane from '../TerminalPane.vue'
import { useTerminalStore } from '../../stores/terminal'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: apiDel },
}))

// ── Fakes ────────────────────────────────────────────────────────────────

/**
 * The screen, stubbed.
 *
 * The real screen is an xterm, and xterm needs a canvas and a layout engine
 * that jsdom does not have. What the pane owes a screen is the contract this
 * file checks: one screen per session, input only while the session is live,
 * a reset when the engine's replay was truncated, and a disposal when the
 * pane gives the screen up.
 */
const screen = vi.hoisted(() => {
  interface Options {
    sessionId: string
    element: HTMLElement
    onInput?: (bytes: Uint8Array) => void
    onResize?: (cols: number, rows: number) => void
  }

  class FakeScreen {
    static instances: FakeScreen[] = []
    // The real screen keeps its session id in a private field, and a private
    // field is not readable through a reactive proxy. The fake does the same,
    // so a pane that wrapped the screen in a deep ref would fail here rather
    // than in a browser.
    readonly #sessionId: string
    #inputEnabled = false
    element: HTMLElement
    disposed = false
    focused = 0
    resets = 0
    writes: Uint8Array[] = []
    onInput: ((bytes: Uint8Array) => void) | null
    onResize: ((cols: number, rows: number) => void) | null

    get sessionId(): string { return this.#sessionId }
    get inputEnabled(): boolean { return this.#inputEnabled }

    constructor(options: Options) {
      this.#sessionId = options.sessionId
      this.element = options.element
      this.onInput = options.onInput ?? null
      this.onResize = options.onResize ?? null
      FakeScreen.instances.push(this)
    }

    setInputEnabled(enabled: boolean): void { this.#inputEnabled = enabled }
    write(bytes: Uint8Array): Promise<void> { this.writes.push(bytes); return Promise.resolve() }
    reset(): void { this.resets += 1 }
    resize(): void {}
    focus(): void { this.focused += 1 }
    refreshTheme(): void {}
    dispose(): void { this.disposed = true }

    /** A keystroke, the way the terminal delivers one. */
    type(text: string): void {
      this.onInput?.(new TextEncoder().encode(text))
    }

    /** The size the screen measured, the way the fit addon reports it. */
    measure(cols: number, rows: number): void {
      this.onResize?.(cols, rows)
    }
  }

  return { FakeScreen, instances: FakeScreen.instances }
})

vi.mock('../../lib/terminalRenderer', () => ({ TerminalRenderer: screen.FakeScreen }))

let sockets: FakeSocket[] = []

class FakeSocket {
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  readyState = FakeSocket.CONNECTING
  bufferedAmount = 0
  sent: string[] = []
  closed = false

  #onopen: (() => void) | null = null
  get onopen() { return this.#onopen }
  set onopen(fn: (() => void) | null) {
    this.#onopen = fn
    if (fn) {
      this.readyState = FakeSocket.OPEN
      fn()
    }
  }

  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null
  onerror: (() => void) | null = null

  constructor(public url: string) {
    sockets.push(this)
  }

  send(data: string): void { this.sent.push(data) }

  close(code?: number): void {
    this.closed = true
    this.readyState = FakeSocket.CLOSED
    this.onclose?.({ code } as CloseEvent)
  }

  /** Deliver one server frame. */
  receive(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }

  frames(): Record<string, unknown>[] {
    return this.sent.map((raw) => JSON.parse(raw) as Record<string, unknown>)
  }

  ofType(type: string): Record<string, unknown>[] {
    return this.frames().filter((frame) => frame.type === type)
  }
}

function statusPayload(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    available: true,
    reason: '',
    platform: 'macos',
    host: 'engine.local',
    unlock_expires_in: 900,
    detached_ttl_seconds: 600,
    ...overrides,
  }
}

function sessionInfo(sessionId: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    session_id: sessionId,
    folder: '/tmp/work',
    cols: 80,
    rows: 24,
    seq: 0,
    attached: false,
    exited: false,
    exit_code: null,
    ...overrides,
  }
}

/** The API the store talks to: a status, a session list, and nothing else. */
function serve(sessions: Record<string, unknown>[] = [], status: Record<string, unknown> = {}) {
  apiGet.mockImplementation((path: string) => {
    if (path === '/api/terminal/status') return Promise.resolve(statusPayload(status))
    if (path.startsWith('/api/terminal/sessions')) return Promise.resolve({ sessions })
    return Promise.resolve({})
  })
}

async function mountPane(props: { inTile?: boolean } = {}) {
  const wrapper = mount(TerminalPane, { props, attachTo: document.body })
  await flushPromises()
  return wrapper
}

/** The width jsdom starts with, so a test that changes it can put it back. */
const DEFAULT_VIEWPORT_WIDTH = window.innerWidth

/**
 * The pane's shape follows the viewport, exactly as the layout's does: a
 * column beside the chat on a wide pane, a full-screen drawer on a phone.
 */
function setViewportWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { writable: true, configurable: true, value: width })
  window.dispatchEvent(new Event('resize'))
}

describe('TerminalPane', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sockets = []
    screen.FakeScreen.instances.length = 0
    apiGet.mockReset()
    apiPost.mockReset()
    apiDel.mockReset()
    apiPost.mockResolvedValue({ session: sessionInfo('s-new') })
    apiDel.mockResolvedValue({ ok: true })
    vi.stubGlobal('WebSocket', FakeSocket)
  })

  afterEach(() => {
    useTerminalStore().dispose()
    document.body.innerHTML = ''
    setViewportWidth(DEFAULT_VIEWPORT_WIDTH)
    vi.unstubAllGlobals()
  })

  it('never renders a password field, not even when the terminal is locked', async () => {
    // The engine refuses the grant, so the pane really is locked — the
    // state the old unlock form used to live in.
    serve([], { unlock_expires_in: 0 })
    apiPost.mockRejectedValue(new Error('refused'))
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    expect(store.phase).toBe('locked')
    // The locked body says what happened, so an empty pane cannot pass
    // for a locked one.
    expect(wrapper.text()).toContain('The terminal grant could not be obtained.')
    // There is no unlock form any more: the signed-in session is the
    // terminal's trust boundary, so the pane has nothing to ask for.
    expect(wrapper.find('.tp-unlock').exists()).toBe(false)
    expect(wrapper.find('input[type="password"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Terminal password')
    expect(wrapper.text()).not.toContain('Unlock')
    wrapper.unmount()
  })

  it('fills the tile when the layout puts it in the slot', async () => {
    serve([sessionInfo('s1')])
    setViewportWidth(1280)
    const store = useTerminalStore()
    const wrapper = await mountPane({ inTile: true })
    await store.open({ chatId: 'c1' })
    await flushPromises()

    const pane = wrapper.get('.terminal-pane')
    expect(pane.classes()).toContain('terminal-pane--tile')
    expect(pane.classes()).not.toContain('terminal-pane--drawer')
    wrapper.unmount()
  })

  it('is a drawer when the layout did not give it the slot', async () => {
    // A tile in a pane that cannot hold it would squeeze the chat to a
    // sliver, so a pane that has no room gets the drawer instead.
    serve([sessionInfo('s1')])
    setViewportWidth(900)
    const store = useTerminalStore()
    const wrapper = await mountPane({ inTile: false })
    await store.open({ chatId: 'c1' })
    await flushPromises()

    const pane = wrapper.get('.terminal-pane')
    expect(pane.classes()).toContain('terminal-pane--drawer')
    expect(pane.classes()).not.toContain('terminal-pane--tile')
    wrapper.unmount()
  })

  it('is a drawer on a phone-sized pane, however much room it is told it has', async () => {
    // A shell on a phone wants the whole screen, whatever the layout says.
    serve([sessionInfo('s1')])
    setViewportWidth(375)
    const store = useTerminalStore()
    const wrapper = await mountPane({ inTile: true })
    await store.open({ chatId: 'c1' })
    await flushPromises()

    const pane = wrapper.get('.terminal-pane')
    expect(pane.classes()).toContain('terminal-pane--drawer')
    expect(pane.classes()).not.toContain('terminal-pane--tile')
    wrapper.unmount()
  })

  it('is a drawer until the layout says it can have the slot', async () => {
    serve([sessionInfo('s1')])
    setViewportWidth(1280)
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    const pane = wrapper.get('.terminal-pane')
    expect(pane.classes()).toContain('terminal-pane--drawer')
    expect(pane.classes()).not.toContain('terminal-pane--tile')
    wrapper.unmount()
  })

  it('obtains the grant itself and lists the sessions', async () => {
    serve([sessionInfo('s1'), sessionInfo('s2', { folder: '/tmp/other', exited: true })], {
      unlock_expires_in: 0,
    })
    apiPost.mockResolvedValue({ ok: true, expires_in: 900 })
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    // The pane asks the engine for the grant by itself, with an empty
    // body: there is no password to ask for.
    expect(apiPost).toHaveBeenCalledWith('/api/terminal/unlock')
    expect(store.unlocked).toBe(true)
    expect(store.phase).not.toBe('locked')
    const rows = wrapper.findAll('.tp-session')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('/tmp/work')
    expect(rows[1].text()).toContain('ended')
    wrapper.unmount()
  })

  it('opens a screen for the session and connects it', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    expect(screen.FakeScreen.instances).toHaveLength(1)
    expect(screen.FakeScreen.instances[0].sessionId).toBe('s1')
    expect(sockets).toHaveLength(1)
    expect(sockets[0].url).toContain('/ws/terminal/s1')
    expect(sockets[0].ofType('attach')).toHaveLength(1)
    wrapper.unmount()
  })

  it('sends a keystroke to the shell and keeps the app out of it', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    sockets[0].receive({
      type: 'ready', session_id: 's1', folder: '/tmp/work', cols: 80, rows: 24,
      seq: 0, replay_start: 0, truncated: false,
    })
    await flushPromises()
    expect(store.phase).toBe('live')

    screen.FakeScreen.instances[0].type('ls\n')
    const inputs = sockets[0].ofType('input')
    expect(inputs).toHaveLength(1)
    expect(inputs[0].data).toBe('bHMK')
    wrapper.unmount()
  })

  it('hides without ending the session', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    await wrapper.get('.tp-hide').trigger('click')
    await flushPromises()

    expect(store.visible).toBe(false)
    // The shell keeps running on the engine: the session is still there, and
    // nothing was asked to end it.
    expect(store.sessions.map((s) => s.session_id)).toEqual(['s1'])
    expect(store.activeSession?.session_id).toBe('s1')
    expect(apiDel).not.toHaveBeenCalled()
    expect(sockets[0].closed).toBe(true)
    wrapper.unmount()
  })

  it('gives the keyboard back to the control that opened the pane', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const opener = document.createElement('button')
    document.body.appendChild(opener)
    opener.focus()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    await wrapper.get('.tp-hide').trigger('click')
    await flushPromises()

    expect(document.activeElement).toBe(opener)
    wrapper.unmount()
  })

  it('ends the session when End is pressed', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    await wrapper.get('.tp-end').trigger('click')
    await flushPromises()

    expect(apiDel).toHaveBeenCalledWith('/api/terminal/sessions/s1')
    expect(store.sessions).toHaveLength(0)
    expect(store.activeSession).toBeNull()
    wrapper.unmount()
  })

  it('offers a takeover when another connection took the session', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    sockets[0].close(4409)
    await flushPromises()

    expect(store.phase).toBe('takeover')
    const panel = wrapper.get('.tp-takeover')
    expect(wrapper.find('.tp-takeover').exists()).toBe(true)
    expect(panel.text()).toContain('Another connection took over')

    await wrapper.get('.tp-takeover-button').trigger('click')
    await flushPromises()

    // The takeover is asked for explicitly, on a new connection.
    expect(sockets).toHaveLength(2)
    const attach = sockets[1].ofType('attach')
    expect(attach).toHaveLength(1)
    expect(attach[0].takeover).toBe(true)
    wrapper.unmount()
  })

  it('keeps the digits and the IME out of the app, and lets the terminal have them', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    const appSaw: string[] = []
    const terminalSaw: string[] = []
    const app = (event: KeyboardEvent) => { appSaw.push(event.key) }
    const terminal = (event: KeyboardEvent) => { terminalSaw.push(event.key) }
    window.addEventListener('keydown', app)
    const surface = wrapper.get('.tp-surface').element as HTMLElement
    surface.addEventListener('keydown', terminal)

    surface.dispatchEvent(new KeyboardEvent('keydown', { key: '1', bubbles: true }))
    surface.dispatchEvent(new KeyboardEvent('keydown', { key: '2', bubbles: true, isComposing: true }))

    // The terminal's own listener ran first, so the shell still gets the key.
    expect(terminalSaw).toEqual(['1', '2'])
    // The app's window listener never saw either of them.
    expect(appSaw).toEqual([])

    window.removeEventListener('keydown', app)
    surface.removeEventListener('keydown', terminal)
    wrapper.unmount()
  })

  it('hands the keyboard back to the pane on Shift+Tab, without sending a byte', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    // The terminal holds the keyboard through its own hidden field, the way
    // xterm does.
    const surface = wrapper.get('.tp-surface').element as HTMLElement
    const field = document.createElement('textarea')
    surface.appendChild(field)
    field.focus()

    field.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true }))
    await flushPromises()

    expect(document.activeElement).toBe(wrapper.get('.tp-hide').element)
    // The shell never saw it, so nothing was sent.
    expect(sockets[0].ofType('input')).toHaveLength(0)
    wrapper.unmount()
  })

  it('resets the screen and says so when the replay was truncated', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    // The engine kept only the tail of its output ring: the screen cannot be
    // rebuilt from what is left.
    sockets[0].receive({
      type: 'ready', session_id: 's1', folder: '/tmp/work', cols: 80, rows: 24,
      seq: 120, replay_start: 40, truncated: true,
    })
    sockets[0].receive({ type: 'output', start: 40, end: 44, data: 'dG91dA==' })
    await flushPromises()

    expect(screen.FakeScreen.instances[0].resets).toBe(1)
    const notice = wrapper.get('.tp-truncated')
    expect(wrapper.find('.tp-truncated').exists()).toBe(true)
    expect(notice.text()).toContain('truncated')
    expect(notice.text()).toContain('cannot rebuild the full screen')
    wrapper.unmount()
  })

  it('says what the terminal is: a shell on the engine, not a sandbox', async () => {
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const wrapper = await mountPane()
    await store.open({ chatId: 'c1' })
    await flushPromises()

    expect(wrapper.get('.tp-warning').text()).toContain('not a sandbox')
    // The status line names the engine, the folder and the platform.
    const status = wrapper.get('.tp-status').text()
    expect(status).toContain('engine.local')
    expect(status).toContain('/tmp/work')
    expect(status).toContain('macos')
    wrapper.unmount()
  })

  it('keeps its controls at 44px on touch', () => {
    // jsdom has no layout engine, so the floor is asserted from the rule the
    // pane ships — and from the shared token, so it cannot drift from the
    // 44px App.vue defines.
    const source = readFileSync(join(__dirname, '..', 'TerminalPane.vue'), 'utf8')
    expect(source).toMatch(/@media \(pointer: coarse\)/)
    expect(source).toMatch(/min-height:\s*var\(--touch\)/)
    expect(source).toMatch(/\.tp-session \{[^}]*min-height:\s*var\(--touch\)/)
  })

  it('draws no frame of its own in the tile: the tile is the window', () => {
    // The slot is the same window the pinned file uses — inset, rounded,
    // shadowed — so the terminal inside it must not draw a second frame.
    // jsdom has no layout engine, so the rule is asserted from the sheet the
    // pane ships.
    const source = readFileSync(join(__dirname, '..', 'TerminalPane.vue'), 'utf8')
    const tile = source.match(/\.terminal-pane--tile\.terminal-pane--open \{([^}]*)\}/)
    expect(tile).not.toBeNull()
    const rules = tile![1]
    expect(rules).toMatch(/height:\s*100%/)
    expect(rules).toMatch(/background:\s*transparent/)
    expect(rules).not.toMatch(/border/)
    expect(rules).not.toMatch(/border-radius/)
    expect(rules).not.toMatch(/margin:/)
    // No shadow of its own either: the window it sits in already has one.
    expect(rules).not.toMatch(/box-shadow/)
    // The flush dock is gone: the tile is the only column shape.
    expect(source).not.toMatch(/terminal-pane--dock/)
  })

  it('binds a screen to the session that is already open when it mounts again', async () => {
    // The pane moves between the slot and the drawer, so it unmounts and
    // mounts again while the shell is still running. The session is still
    // the one the screen belongs to, and the new pane must draw it — a
    // second screen for the same session would be two histories at once.
    serve([sessionInfo('s1')])
    const store = useTerminalStore()
    const first = await mountPane({ inTile: true })
    await store.open({ chatId: 'c1' })
    await flushPromises()

    expect(screen.FakeScreen.instances).toHaveLength(1)
    expect(screen.FakeScreen.instances[0].sessionId).toBe('s1')
    expect(screen.FakeScreen.instances[0].disposed).toBe(false)
    const firstElement = screen.FakeScreen.instances[0].element

    first.unmount()
    await flushPromises()
    // The old screen goes with the old pane.
    expect(screen.FakeScreen.instances[0].disposed).toBe(true)
    expect(firstElement.isConnected).toBe(false)

    const second = await mountPane({ inTile: false })
    await flushPromises()

    // The new pane made its own screen, and it is bound to the same
    // session the old one drew.
    expect(screen.FakeScreen.instances).toHaveLength(2)
    expect(screen.FakeScreen.instances[1].sessionId).toBe('s1')
    expect(screen.FakeScreen.instances[1].disposed).toBe(false)
    // It is a live screen in the live DOM, not a dead pane.
    expect(screen.FakeScreen.instances[1].element.isConnected).toBe(true)
    expect(screen.FakeScreen.instances[1].element).not.toBe(firstElement)
    // And the store is drawing on it again: the session is attached, so the
    // pane reconnects to it.
    expect(store.activeSession?.session_id).toBe('s1')
    expect(sockets).toHaveLength(2)
    expect(sockets[1].closed).toBe(false)
    second.unmount()
  })
})
