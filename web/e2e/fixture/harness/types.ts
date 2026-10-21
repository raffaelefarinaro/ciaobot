/**
 * The control surface the terminal harness exposes on `window`.
 *
 * The harness is a test-only page: it exists so a real browser can drive the
 * real renderer. This file is the contract between the page and the spec
 * that reads it.
 */

export interface TerminalHarness {
  /** Write text to the screen; resolves with the acknowledged end offset. */
  write(text: string): Promise<number>
  /** Turn keystrokes on or off. */
  setInputEnabled(enabled: boolean): void
  /** Put the keyboard on the screen. */
  focus(): void
  /** The screen's size in character cells, as the renderer last measured it. */
  size(): { cols: number; rows: number }
  /** Every keystroke that reached the screen, as byte lists. */
  inputs(): number[][]
  /** Every acknowledged end offset, in order. */
  acks(): number[]
  /** Tear the screen down. */
  dispose(): void
  /** True once the screen was disposed. */
  disposed(): boolean
}

declare global {
  interface Window {
    __terminal: TerminalHarness
  }
}
