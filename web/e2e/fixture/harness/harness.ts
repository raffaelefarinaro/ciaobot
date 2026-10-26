/**
 * The terminal renderer's browser harness.
 *
 * A test-only page: it mounts the real renderer on a bare div and exposes
 * the few things a browser spec needs to drive it. There is no product route
 * here and no copy for a person to read — the page exists so a real browser
 * can prove what jsdom cannot: that the screen draws what was written to
 * it, that a keystroke reaches the shell only while input is enabled, that
 * a resize changes the window, and that a disposed screen leaves nothing
 * behind.
 */

import { TerminalRenderer } from '../../../src/lib/terminalRenderer'
import type { TerminalHarness } from './types'

const host = document.getElementById('terminal')
if (!host) throw new Error('The terminal harness needs a #terminal element to mount on.')

const encoder = new TextEncoder()
const acks: number[] = []
const inputs: number[][] = []
let size = { cols: 0, rows: 0 }
let disposed = false

const renderer = new TerminalRenderer({
  sessionId: 'harness-session',
  element: host,
  onInput: (bytes) => {
    inputs.push(Array.from(bytes))
  },
  onResize: (cols, rows) => {
    size = { cols, rows }
  },
})

const harness: TerminalHarness = {
  write(text: string): Promise<number> {
    return new Promise<number>((resolve, reject) => {
      renderer.write(encoder.encode(text), (end) => {
        acks.push(end)
        resolve(end)
      }).catch(reject)
    })
  },
  setInputEnabled(enabled: boolean): void {
    renderer.setInputEnabled(enabled)
  },
  focus(): void {
    renderer.focus()
  },
  size(): { cols: number; rows: number } {
    return size
  },
  inputs(): number[][] {
    return inputs
  },
  acks(): number[] {
    return acks
  },
  dispose(): void {
    disposed = true
    renderer.dispose()
  },
  disposed(): boolean {
    return disposed
  },
}

Object.defineProperty(window, '__terminal', {
  configurable: true,
  value: harness,
})
