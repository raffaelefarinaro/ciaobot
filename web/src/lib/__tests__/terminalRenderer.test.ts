// @vitest-environment jsdom

/**
 * The terminal screen, in jsdom.
 *
 * xterm is stubbed here on purpose: jsdom has no layout engine, so the
 * fit, the cell metrics and the rendering are not facts this environment
 * can establish. What *is* a fact here is the contract around the screen —
 * the session it is bound to, when a keystroke is allowed through, what an
 * acknowledgement carries, and what a disposal does to a write that was
 * never parsed. The browser spec proves the rest against the real xterm.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TerminalRenderer, readTheme } from '../terminalRenderer'

const stubs = vi.hoisted(() => {
  interface FakeOptions {
    scrollback?: number
    disableStdin?: boolean
    linkHandler?: { activate: (event: MouseEvent, text: string) => void }
    fontFamily?: string
    fontSize?: number
    theme?: Record<string, string>
  }

  class FakeTerminal {
    static instances: FakeTerminal[] = []
    options: FakeOptions
    element: HTMLElement | null = null
    writes: Array<{ bytes: Uint8Array; callback?: () => void }> = []
    resets = 0
    disposed = false
    focused = 0
    size = { cols: 80, rows: 24 }
    dataListeners: Array<(data: string) => void> = []
    addons: unknown[] = []

    constructor(options: FakeOptions = {}) {
      this.options = options
      FakeTerminal.instances.push(this)
    }

    open(element: HTMLElement): void {
      this.element = element
    }

    loadAddon(addon: unknown): void {
      this.addons.push(addon)
    }

    onData(listener: (data: string) => void): { dispose(): void } {
      this.dataListeners.push(listener)
      return { dispose: () => undefined }
    }

    write(bytes: Uint8Array, callback?: () => void): void {
      this.writes.push({ bytes, callback })
    }

    reset(): void {
      this.resets += 1
    }

    resize(cols: number, rows: number): void {
      this.size = { cols, rows }
    }

    focus(): void {
      this.focused += 1
    }

    dispose(): void {
      this.disposed = true
    }

    /** A keystroke, the way xterm delivers one while stdin is enabled. */
    type(text: string): void {
      if (this.options.disableStdin) return
      for (const listener of this.dataListeners) listener(text)
    }
  }

  class FakeFitAddon {
    static instances: FakeFitAddon[] = []
    fitCalls = 0

    constructor() {
      FakeFitAddon.instances.push(this)
    }

    activate(): void {
      /* the addon only needs the terminal, which the stub hands it */
    }

    dispose(): void {
      /* nothing to release */
    }

    fit(): void {
      this.fitCalls += 1
    }

    proposeDimensions(): { cols: number; rows: number } {
      return { cols: 80, rows: 24 }
    }
  }

  class FakeResizeObserver {
    static instances: FakeResizeObserver[] = []
    observed: Element[] = []
    disconnected = false

    constructor(public callback: () => void) {
      FakeResizeObserver.instances.push(this)
    }

    observe(element: Element): void {
      this.observed.push(element)
    }

    unobserve(): void {
      /* the stub never stops watching one element */
    }

    disconnect(): void {
      this.disconnected = true
    }
  }

  return { FakeTerminal, FakeFitAddon, FakeResizeObserver }
})

vi.mock('@xterm/xterm', () => ({ Terminal: stubs.FakeTerminal }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: stubs.FakeFitAddon }))

/** The screen the renderer built, once it exists. */
async function waitForTerminal(): Promise<InstanceType<typeof stubs.FakeTerminal>> {
  await vi.waitFor(() => {
    expect(stubs.FakeTerminal.instances.length).toBeGreaterThan(0)
  })
  return stubs.FakeTerminal.instances[0]
}

/**
 * Answer `getComputedStyle` the way a browser would.
 *
 * The host answers with the tokens it defines; the throwaway probe the
 * renderer uses to resolve one answers with the value the browser computed
 * from it.
 */
function stubComputedStyle(
  element: HTMLElement,
  values: { tokens: Record<string, string>; computed: Record<string, string> },
): void {
  vi.spyOn(window, 'getComputedStyle').mockImplementation((target: Element) => {
    const source = target === element ? values.tokens : values.computed
    const style = document.createElement('div').style
    style.getPropertyValue = (name: string) => source[name] ?? ''
    return style
  })
}

/** A host element the renderer can mount on. */
function host(): HTMLElement {
  const element = document.createElement('div')
  document.body.appendChild(element)
  return element
}

beforeEach(() => {
  stubs.FakeTerminal.instances = []
  stubs.FakeFitAddon.instances = []
  stubs.FakeResizeObserver.instances = []
  Object.defineProperty(globalThis, 'ResizeObserver', {
    configurable: true,
    writable: true,
    value: stubs.FakeResizeObserver,
  })
  document.body.innerHTML = ''
})

afterEach(() => {
  vi.restoreAllMocks()
  Reflect.deleteProperty(globalThis, 'ResizeObserver')
})

describe('the session a screen is bound to', () => {
  it('binds the id and offers no way to rebind it', async () => {
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    await waitForTerminal()

    expect(renderer.sessionId).toBe('session-a')
    const descriptor = Object.getOwnPropertyDescriptor(
      Object.getPrototypeOf(renderer),
      'sessionId',
    )
    expect(descriptor?.get).toBeTypeOf('function')
    expect(descriptor?.set).toBeUndefined()
    const rebindable = renderer as unknown as { sessionId: string }
    expect(() => {
      rebindable.sessionId = 'session-b'
    }).toThrow()
  })

  it('gives every renderer its own screen', async () => {
    const first = host()
    const second = host()
    const a = new TerminalRenderer({ sessionId: 'a', element: first })
    const b = new TerminalRenderer({ sessionId: 'b', element: second })

    await vi.waitFor(() => {
      expect(stubs.FakeTerminal.instances).toHaveLength(2)
    })
    expect(a.sessionId).toBe('a')
    expect(b.sessionId).toBe('b')
    expect(stubs.FakeTerminal.instances[0]).not.toBe(stubs.FakeTerminal.instances[1])
    expect(stubs.FakeTerminal.instances[0].element).toBe(first)
    expect(stubs.FakeTerminal.instances[1].element).toBe(second)
  })
})

describe('input', () => {
  it('sends no keystroke until the caller enables input', async () => {
    const inputs: number[][] = []
    const renderer = new TerminalRenderer({
      sessionId: 'session-a',
      element: host(),
      onInput: (bytes) => inputs.push(Array.from(bytes)),
    })
    const terminal = await waitForTerminal()

    terminal.type('a')
    expect(inputs).toEqual([])
    expect(terminal.options.disableStdin).toBe(true)

    renderer.setInputEnabled(true)
    expect(terminal.options.disableStdin).toBe(false)
    terminal.type('a')
    expect(inputs).toEqual([[97]])
  })

  it('turns input off again', async () => {
    const inputs: number[][] = []
    const renderer = new TerminalRenderer({
      sessionId: 'session-a',
      element: host(),
      onInput: (bytes) => inputs.push(Array.from(bytes)),
    })
    const terminal = await waitForTerminal()

    renderer.setInputEnabled(true)
    renderer.setInputEnabled(false)
    terminal.type('a')
    expect(inputs).toEqual([])
  })
})

describe('the acknowledgement', () => {
  it('carries the absolute end offset of the bytes just parsed', async () => {
    const acks: number[] = []
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()

    renderer.write(new Uint8Array([1, 2, 3]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })
    terminal.writes[0].callback?.()
    expect(acks).toEqual([3])

    renderer.write(new Uint8Array([4, 5]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(2)
    })
    terminal.writes[1].callback?.()
    expect(acks).toEqual([3, 5])
  })

  it('resolves the promise it returned when the bytes are parsed', async () => {
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()
    const settled = renderer.write(new Uint8Array([1]), () => undefined)

    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })
    terminal.writes[0].callback?.()
    await expect(settled).resolves.toBeUndefined()
  })

  it('writes without a callback, as the store calls it', async () => {
    // The client store's renderer shape is `write(bytes): Promise<void>`:
    // the acknowledgement is the promise, and the callback is optional.
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()
    const settled = renderer.write(new Uint8Array([1, 2]))

    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })
    terminal.writes[0].callback?.()
    await expect(settled).resolves.toBeUndefined()
  })
})

describe('disposal', () => {
  it('rejects a write that was never parsed and leaves the offset alone', async () => {
    const acks: number[] = []
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()

    renderer.write(new Uint8Array([1, 2, 3]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })
    terminal.writes[0].callback?.()
    expect(acks).toEqual([3])

    const pending = renderer.write(new Uint8Array([4, 5]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(2)
    })
    renderer.dispose()

    await expect(pending).rejects.toThrow()
    expect(acks).toEqual([3])
    expect(terminal.disposed).toBe(true)
  })

  it('refuses a write after the screen was disposed', async () => {
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    await waitForTerminal()
    renderer.dispose()

    await expect(renderer.write(new Uint8Array([1]), () => undefined)).rejects.toThrow()
  })

  it('ignores a callback that arrives after the screen was disposed', async () => {
    const acks: number[] = []
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()
    const pending = renderer.write(new Uint8Array([1, 2]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })

    renderer.dispose()
    terminal.writes[0].callback?.()

    expect(acks).toEqual([])
    await expect(pending).rejects.toThrow()
  })

  it('disconnects the observer it owns', async () => {
    const element = host()
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element })
    await waitForTerminal()

    expect(stubs.FakeResizeObserver.instances).toHaveLength(1)
    expect(stubs.FakeResizeObserver.instances[0].observed).toEqual([element])
    renderer.dispose()
    expect(stubs.FakeResizeObserver.instances[0].disconnected).toBe(true)
  })
})

describe('the theme', () => {
  it('resolves a token to a colour xterm can parse', () => {
    const element = host()
    stubComputedStyle(element, {
      tokens: { '--bg2': '#1f2240' },
      computed: { color: 'rgb(31, 34, 64)' },
    })

    const theme = readTheme(element)
    expect(theme?.background).toBe('rgb(31, 34, 64)')
  })

  it('refuses a color-mix() value instead of passing it to xterm', () => {
    const element = host()
    stubComputedStyle(element, {
      tokens: { '--bg2': 'color-mix(in srgb, #fb923c 50%, #1a1a2e)' },
      computed: { color: 'color(srgb 0.98 0.57 0.24)' },
    })

    const theme = readTheme(element)
    expect(theme?.background).toBeUndefined()
    expect(JSON.stringify(theme)).not.toContain('color-mix')
  })

  it('leaves out a token the page does not define', () => {
    const element = host()
    stubComputedStyle(element, { tokens: {}, computed: { color: 'rgb(0, 0, 0)' } })

    expect(readTheme(element)).toBeNull()
  })

  it('applies the theme and fits once when the font changes', async () => {
    const element = host()
    stubComputedStyle(element, {
      tokens: {
        '--bg2': '#1f2240',
        '--font-mono': 'ui-monospace, monospace',
        '--text-base': 'calc(14px * 1.2)',
      },
      computed: { color: 'rgb(31, 34, 64)', 'font-size': '16.8px' },
    })
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element })
    const terminal = await waitForTerminal()
    const addon = stubs.FakeFitAddon.instances[0]
    const fitsBefore = addon.fitCalls

    renderer.refreshTheme()

    expect(addon.fitCalls).toBe(fitsBefore + 1)
    expect(terminal.options.theme?.background).toBe('rgb(31, 34, 64)')
    expect(terminal.options.fontFamily).toBe('ui-monospace, monospace')
    expect(terminal.options.fontSize).toBe(16.8)
  })
})

describe('the screen itself', () => {
  it('keeps a thousand lines of scrollback and never follows a link', async () => {
    const terminal = await (async () => {
      const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
      const created = await waitForTerminal()
      renderer.dispose()
      return created
    })()

    expect(terminal.options.scrollback).toBe(1000)
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    terminal.options.linkHandler?.activate(new MouseEvent('click'), 'https://example.com')
    expect(opened).not.toHaveBeenCalled()
  })

  it('reaches xterm only through a dynamic import', () => {
    const source = readFileSync(resolve(import.meta.dirname, '../terminalRenderer.ts'), 'utf8')
    expect(source).not.toMatch(/from\s+'@xterm/)
  })
})

describe('a reset', () => {
  it('clears the screen and writes from zero again', async () => {
    const acks: number[] = []
    const renderer = new TerminalRenderer({ sessionId: 'session-a', element: host() })
    const terminal = await waitForTerminal()

    renderer.write(new Uint8Array([1, 2, 3]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(1)
    })
    terminal.writes[0].callback?.()
    expect(acks).toEqual([3])

    renderer.reset()
    expect(terminal.resets).toBe(1)
    expect(() => renderer.reset()).not.toThrow()

    renderer.write(new Uint8Array([4, 5]), (end) => acks.push(end))
    await vi.waitFor(() => {
      expect(terminal.writes).toHaveLength(2)
    })
    terminal.writes[1].callback?.()
    expect(acks).toEqual([3, 2])
  })
})
