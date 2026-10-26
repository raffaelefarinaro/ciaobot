/**
 * The embedded terminal's screen: one xterm bound to one session, for life.
 *
 * The pane that shows a terminal owns the *session*; this module owns the
 * *screen*. A screen is a physical thing: one xterm instance, one parser,
 * one buffer, one stream of bytes in the order they arrived. Two sessions
 * cannot share it, so the session id is fixed at construction and there is
 * no way to rebind it — a renderer that has to show another session is a
 * different renderer.
 *
 * Four rules shape the code below.
 *
 * Input is off until the caller turns it on. A screen that is not
 * controlling a session must not send a single byte to it, so `disableStdin`
 * starts on and only `setInputEnabled(true)` lifts it. The xterm options
 * object never leaves this module: there is no third-party options object to
 * mutate from outside, and no cast is used to undo `readonly`.
 *
 * An acknowledgement is real. `write` acknowledges only from xterm's own
 * write callback, and the number it carries is the absolute end offset of
 * the bytes that were just parsed. A write that is still queued or in
 * flight when the screen is disposed is *rejected*: disposal does not
 * advance an acknowledged offset, and a late callback from a disposed
 * screen is ignored.
 *
 * The theme comes from the app's own tokens, resolved to colours xterm can
 * actually parse. A custom property is not resolved by `getComputedStyle` —
 * it answers with the token's own value, which may still be a `var()`
 * reference or a `color-mix()`. xterm's colour parser knows `rgb()` and
 * `rgba()` and nothing else, so a value that the browser computes into
 * anything else is refused rather than passed on.
 *
 * The screen is measured by one ResizeObserver, owned here and disconnected
 * on dispose. The font decides the cell size, so a font change re-reads the
 * tokens and fits once.
 *
 * xterm and its fit addon are imported dynamically, so neither enters the
 * bundle of a page that does not show a terminal: Home's payload is
 * unchanged by this module existing.
 */

import {
  TERMINAL_MAX_COLS,
  TERMINAL_MAX_ROWS,
  TERMINAL_MIN_COLS,
  TERMINAL_MIN_ROWS,
} from './terminalProtocol'
// The terminal's own stylesheet travels with the terminal: the screen is
// unreadable without it, and the pane that shows a screen should not have
// to know that.
import '@xterm/xterm/css/xterm.css'

type XtermTerminal = import('@xterm/xterm').Terminal
type XtermTheme = import('@xterm/xterm').ITheme
type FitAddon = import('@xterm/addon-fit').FitAddon
type XtermTerminalConstructor = typeof import('@xterm/xterm').Terminal
type FitAddonConstructor = typeof import('@xterm/addon-fit').FitAddon

/**
 * The xterm modules, loaded once.
 *
 * The promise is kept so two screens created in the same tick share one
 * import: the modules are fetched once per page, not once per screen.
 */
let xtermModules: Promise<{
  Terminal: XtermTerminalConstructor
  FitAddon: FitAddonConstructor
}> | null = null

/** Load the terminal's modules, once. */
function loadXtermModules(): Promise<{
  Terminal: XtermTerminalConstructor
  FitAddon: FitAddonConstructor
}> {
  if (!xtermModules) {
    xtermModules = (async () => {
      const [xterm, fit] = await Promise.all([
        import('@xterm/xterm'),
        import('@xterm/addon-fit'),
      ])
      return { Terminal: xterm.Terminal, FitAddon: fit.FitAddon }
    })()
  }
  return xtermModules
}

/** The lines of scrollback the screen keeps. */
export const TERMINAL_SCROLLBACK = 1000

/** The token the terminal's font family comes from. */
const FONT_FAMILY_TOKEN = '--font-mono'
/** The token the terminal's font size comes from. */
const FONT_SIZE_TOKEN = '--text-base'
/** The font family used when the page has no mono token of its own. */
const DEFAULT_FONT_FAMILY = 'monospace'
/** The font size used when the page has no size token of its own. */
const DEFAULT_FONT_SIZE = 13

/** The encoder every keystroke goes through. */
const ENCODER = new TextEncoder()

/**
 * A link handler that does nothing.
 *
 * The screen shows a shell's output; a URL in it is output, not a
 * destination. xterm only follows a link when a handler says so, and this
 * one says nothing: no navigation, no new tab, no history entry.
 */
const INERT_LINK_HANDLER = {
  activate(): void {
    /* a link in the output is output */
  },
}

/**
 * The app's colour tokens, and the theme keys they fill.
 *
 * The app's palette is indigo surfaces, an orange accent, a violet
 * secondary and three semantic colours. It has no blue and no cyan, so the
 * ANSI slots it cannot answer for keep xterm's own palette, which
 * `minimumContrastRatio` lifts to a legible contrast against the app's
 * background rather than leaving them dark on dark.
 */
type ThemeKey = Exclude<keyof XtermTheme, 'extendedAnsi'>

const THEME_TOKENS: ReadonlyArray<readonly [ThemeKey, string]> = [
  ['background', '--bg2'],
  ['foreground', '--fg'],
  ['cursor', '--accent'],
  ['cursorAccent', '--on-accent'],
  ['selectionBackground', '--accent2'],
  ['black', '--bg3'],
  ['red', '--error'],
  ['green', '--success'],
  ['yellow', '--warning'],
  ['white', '--fg2'],
  ['brightBlack', '--fg3'],
  ['brightRed', '--error'],
  ['brightGreen', '--success'],
  ['brightYellow', '--warning'],
  ['brightWhite', '--fg'],
]

/** What the renderer needs to put a screen on a page. */
export interface TerminalRendererOptions {
  /** The session this screen shows, for the renderer's whole life. */
  sessionId: string
  /** The element the terminal is mounted in. */
  element: HTMLElement
  /** Called with the bytes a keystroke produced. */
  onInput?: (bytes: Uint8Array) => void
  /** Called with the screen's size after it was measured. */
  onResize?: (cols: number, rows: number) => void
}

/** One write, from the moment it is accepted to the moment it is parsed. */
interface PendingWrite {
  /** The bytes to write. */
  bytes: Uint8Array
  /** The absolute end offset these bytes reach. */
  end: number
  /** The caller's acknowledgement, when it asked for one. */
  onAcknowledged?: (end: number) => void
  /** Settles the promise `write` returned. */
  resolve: () => void
  /** Fails the promise `write` returned. */
  reject: (error: Error) => void
}

/**
 * Read the app's colour tokens as a theme xterm can parse.
 *
 * Every token is resolved on its own: a token the page does not define, or
 * one whose value the browser computes into something xterm cannot read, is
 * left out of the theme rather than guessed at. `null` means the page has no
 * colours to give — the caller keeps what it had.
 */
export function readTheme(element: HTMLElement): XtermTheme | null {
  const theme: XtermTheme = {}
  let resolved = 0
  for (const [key, token] of THEME_TOKENS) {
    const colour = resolveColour(element, token)
    if (!colour) continue
    theme[key] = colour
    resolved += 1
  }
  return resolved > 0 ? theme : null
}

/**
 * One physical screen for one terminal session.
 *
 * The session id is a getter with no setter: a screen that showed another
 * session would be a screen whose bytes are two histories at once.
 */
export class TerminalRenderer {
  /** The session this screen shows. It never changes. */
  get sessionId(): string {
    return this.#sessionId
  }

  readonly #sessionId: string
  readonly #element: HTMLElement
  readonly #onInput: ((bytes: Uint8Array) => void) | null
  readonly #onResize: ((cols: number, rows: number) => void) | null

  /** The xterm, once it exists. */
  #terminal: XtermTerminal | null = null
  /** The fit addon, once it exists. */
  #fitAddon: FitAddon | null = null
  /** The one observer that watches the host's size. */
  #observer: ResizeObserver | null = null

  /** True once `dispose` ran. A disposed screen is never reused. */
  #disposed = false
  /** Why the screen could not be started, when it could not be. */
  #failure: Error | null = null
  /** True while the screen may send keystrokes to the session. */
  #inputEnabled = false
  /** The absolute end offset the next accepted write reaches. */
  #nextOffset = 0
  /** Writes waiting for the terminal, or for their turn. */
  #queue: PendingWrite[] = []
  /** Writes handed to the terminal and not parsed yet. */
  #inFlight = new Set<PendingWrite>()
  /** The size the caller asked for. */
  #size: { cols: number; rows: number } | null = null
  /** True when a reset was asked for before the terminal existed. */
  #needsReset = false

  constructor(options: TerminalRendererOptions) {
    this.#sessionId = options.sessionId
    this.#element = options.element
    this.#onInput = options.onInput ?? null
    this.#onResize = options.onResize ?? null
    // A screen that nobody has claimed sends nothing. The pane turns input
    // on when the session is controlling, unlocked and live.
    this.setInputEnabled(false)
    void this.#create()
  }

  /**
   * Turn keystrokes on or off.
   *
   * This is the only way input changes, and it changes nothing else: the
   * xterm options object stays inside this module.
   */
  setInputEnabled(enabled: boolean): void {
    if (this.#disposed) return
    this.#inputEnabled = enabled
    this.#applyInputState()
  }

  /**
   * Set the screen's size, in character cells.
   *
   * The size is clamped to the engine's bounds: a frame outside them is
   * refused by the engine, and a screen that disagrees with the engine is a
   * screen the shell cannot draw into.
   */
  resize(cols: number, rows: number): void {
    if (this.#disposed) return
    this.#size = {
      cols: clampCells(cols, TERMINAL_MIN_COLS, TERMINAL_MAX_COLS),
      rows: clampCells(rows, TERMINAL_MIN_ROWS, TERMINAL_MAX_ROWS),
    }
    this.#applySize()
  }

  /**
   * Write bytes to the screen.
   *
   * The acknowledgement fires from xterm's own write callback and carries
   * the absolute end offset of the bytes that were just parsed. The promise
   * settles when that happens, and is *rejected* if the screen is disposed
   * before the bytes are parsed: an unparsed write is not an acknowledged
   * one, and a disposed screen must not pretend otherwise.
   */
  write(
    bytes: Uint8Array,
    onAcknowledged?: (end: number) => void,
  ): Promise<void> {
    if (this.#disposed) {
      return refused('The terminal screen was disposed before the write was parsed.')
    }
    if (this.#failure) {
      return refused(this.#failure.message)
    }
    const end = this.#nextOffset + bytes.length
    this.#nextOffset = end
    let resolve!: () => void
    let reject!: (error: Error) => void
    const promise = new Promise<void>((res, rej) => {
      resolve = res
      reject = rej
    })
    // The caller owns this rejection. This keeps a caller that ignores the
    // promise from turning a disposal into an unhandled rejection.
    promise.catch(() => undefined)
    this.#queue.push({ bytes, end, onAcknowledged, resolve, reject })
    this.#pump()
    return promise
  }

  /**
   * Clear the screen and reset the parser.
   *
   * A caller that resets replays from offset zero: the bytes after a reset
   * are the first bytes of a new history.
   */
  reset(): void {
    if (this.#disposed) return
    this.#nextOffset = 0
    const terminal = this.#terminal
    if (!terminal) {
      this.#needsReset = true
      return
    }
    terminal.reset()
  }

  /**
   * Re-read the app's tokens and apply them.
   *
   * A theme switch changes the colours; a font change changes the cell
   * size. Both are measured again here, and the screen is fitted once,
   * because a screen measured with the old metrics reports a size the
   * engine was never told about.
   */
  refreshTheme(): void {
    if (this.#disposed) return
    const terminal = this.#terminal
    if (!terminal) return
    const font = readFont(this.#element)
    terminal.options.fontFamily = font.fontFamily
    terminal.options.fontSize = font.fontSize
    const theme = readTheme(this.#element)
    if (theme) terminal.options.theme = theme
    this.#fit()
  }

  /** Put the keyboard on the screen. */
  focus(): void {
    if (this.#disposed) return
    this.#terminal?.focus()
  }

  /**
   * Tear the screen down.
   *
   * The observer is disconnected, the terminal is disposed, and every write
   * that has not been parsed is rejected: disposal does not advance an
   * acknowledged offset, so a caller that resumes from the last
   * acknowledgement replays the bytes it never saw applied. A disposed
   * screen is never reused, and a callback that arrives late is ignored.
   */
  dispose(): void {
    if (this.#disposed) return
    this.#disposed = true
    this.#observer?.disconnect()
    this.#observer = null
    const pending = this.#queue.splice(0, this.#queue.length)
    for (const write of this.#inFlight) pending.push(write)
    this.#inFlight.clear()
    for (const write of pending) {
      write.reject(new Error('The terminal screen was disposed before the write was parsed.'))
    }
    this.#terminal?.dispose()
    this.#terminal = null
    this.#fitAddon = null
  }

  /** Start the terminal, once the modules are loaded. */
  async #create(): Promise<void> {
    try {
      const { Terminal, FitAddon: Fit } = await loadXtermModules()
      if (this.#disposed) return
      const font = readFont(this.#element)
      const terminal = new Terminal({
        scrollback: TERMINAL_SCROLLBACK,
        disableStdin: true,
        linkHandler: INERT_LINK_HANDLER,
        fontFamily: font.fontFamily,
        fontSize: font.fontSize,
        minimumContrastRatio: 4.5,
        theme: readTheme(this.#element) ?? {},
      })
      const fitAddon = new Fit()
      terminal.loadAddon(fitAddon)
      this.#terminal = terminal
      this.#fitAddon = fitAddon
      terminal.open(this.#element)
      terminal.onData((data) => {
        this.#onInput?.(ENCODER.encode(data))
      })
      this.#watchSize()
      if (this.#needsReset) {
        this.#needsReset = false
        terminal.reset()
      }
      this.#applyInputState()
      this.#applySize()
      this.#fit()
      this.#pump()
    } catch (error) {
      this.#failure = error instanceof Error ? error : new Error('The terminal could not be started.')
      // A screen that could not start leaves nothing behind: the terminal it
      // managed to build is disposed rather than left half-open.
      this.#observer?.disconnect()
      this.#observer = null
      this.#terminal?.dispose()
      this.#terminal = null
      this.#fitAddon = null
      const pending = this.#queue.splice(0, this.#queue.length)
      for (const write of pending) write.reject(this.#failure)
    }
  }

  /** Watch the host's size. One observer, owned here, disconnected on dispose. */
  #watchSize(): void {
    if (typeof ResizeObserver === 'undefined') return
    this.#observer = new ResizeObserver(() => {
      this.#fit()
    })
    this.#observer.observe(this.#element)
  }

  /** Hand the queued writes to the terminal. */
  #pump(): void {
    if (this.#disposed) return
    const terminal = this.#terminal
    if (!terminal) return
    while (this.#queue.length > 0) {
      const pending = this.#queue.shift()
      if (!pending) break
      this.#inFlight.add(pending)
      terminal.write(pending.bytes, () => {
        this.#acknowledge(pending)
      })
    }
  }

  /** The bytes were parsed: acknowledge them, once. */
  #acknowledge(pending: PendingWrite): void {
    if (!this.#inFlight.delete(pending)) return
    if (this.#disposed) return
    pending.resolve()
    try {
      pending.onAcknowledged?.(pending.end)
    } catch (error) {
      // A throwing acknowledgement must not break the terminal's parse loop.
      queueMicrotask(() => {
        throw error
      })
    }
  }

  /** Apply the input state to the terminal, once it exists. */
  #applyInputState(): void {
    const terminal = this.#terminal
    if (!terminal) return
    terminal.options.disableStdin = !this.#inputEnabled
  }

  /** Apply the size the caller asked for, once the terminal exists. */
  #applySize(): void {
    const terminal = this.#terminal
    const size = this.#size
    if (!terminal || !size) return
    terminal.resize(size.cols, size.rows)
  }

  /**
   * Measure the screen against the host.
   *
   * A host that has no size yet proposes a window smaller than the
   * engine's minimum; that is not a window, and fitting it would replace a
   * usable screen with a two-column one.
   */
  #fit(): void {
    const terminal = this.#terminal
    const fitAddon = this.#fitAddon
    if (!terminal || !fitAddon) return
    const proposed = fitAddon.proposeDimensions()
    if (!proposed) return
    if (proposed.cols < TERMINAL_MIN_COLS || proposed.rows < TERMINAL_MIN_ROWS) return
    fitAddon.fit()
    this.#onResize?.(terminal.cols, terminal.rows)
  }
}

/**
 * Resolve one colour token to a value xterm can parse.
 *
 * A custom property is not resolved by `getComputedStyle`: it answers with
 * the token's own value, which may still be a `var()` reference or a
 * `color-mix()`. xterm's colour parser knows `rgb()` and `rgba()` and
 * nothing else, so the value is handed to the browser to compute and the
 * computed result is read back. A value that computes into anything else —
 * `color-mix()` becomes `color(srgb …)` — is refused rather than passed on.
 */
function resolveColour(element: HTMLElement, token: string): string | null {
  const raw = tokenValue(element, token)
  if (!raw) return null
  if (raw.includes('color-mix(')) return null
  const computed = computedValue(element, 'color', raw)
  if (!/^rgba?\(/i.test(computed)) return null
  return computed
}

/**
 * The font the terminal draws with, taken from the app's tokens.
 *
 * The font decides the cell size, so it is read the same way the colours
 * are: the token says what the page wants, the browser says how many
 * pixels that is.
 */
function readFont(element: HTMLElement): { fontFamily: string; fontSize: number } {
  const family = tokenValue(element, FONT_FAMILY_TOKEN) || DEFAULT_FONT_FAMILY
  const rawSize = tokenValue(element, FONT_SIZE_TOKEN)
  const size = rawSize ? Number.parseFloat(computedValue(element, 'font-size', rawSize)) : Number.NaN
  return {
    fontFamily: family,
    fontSize: Number.isFinite(size) && size > 0 ? size : DEFAULT_FONT_SIZE,
  }
}

/** A token's own value, or an empty string when the page has no such token. */
function tokenValue(element: HTMLElement, token: string): string {
  const view = element.ownerDocument.defaultView
  if (!view) return ''
  return view.getComputedStyle(element).getPropertyValue(token).trim()
}

/**
 * Let the browser compute a value.
 *
 * A throwaway probe carries the value in a real property, because only the
 * browser can turn a colour expression into the `rgb()` it paints. The
 * probe never shows: it is `display: none` and it is removed again before
 * this returns.
 */
function computedValue(
  element: HTMLElement,
  property: 'color' | 'font-size',
  raw: string,
): string {
  const doc = element.ownerDocument
  const probe = doc.createElement('span')
  probe.style.display = 'none'
  probe.style.setProperty(property, raw)
  element.appendChild(probe)
  const view = doc.defaultView
  const computed = view ? view.getComputedStyle(probe).getPropertyValue(property).trim() : ''
  probe.remove()
  return computed
}

/** A cell count inside the engine's bounds. */
function clampCells(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(Math.max(Math.round(value), min), max)
}

/** A promise that is already rejected, and already handled. */
function refused(message: string): Promise<void> {
  const promise = Promise.reject(new Error(message))
  promise.catch(() => undefined)
  return promise
}
