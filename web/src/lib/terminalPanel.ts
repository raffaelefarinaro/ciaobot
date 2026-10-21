import { ref } from 'vue'

/**
 * Whether the browser terminal's panel has been asked for.
 *
 * The panel is a chunk of its own, and the app shell must not download it
 * for a page that never opens a terminal: Home's payload is guarded, and the
 * terminal store is heavier than the panel needs to be next to it. So the
 * shell reads this one-bit signal, and the panel is mounted the first time
 * it is true.
 *
 * The signal says "mount the panel", not "the terminal is open". The store
 * still owns that: `visible` is the truth about the pane being on screen.
 */
export const terminalPanelWanted = ref(false)

/** Ask for the terminal panel to be mounted. */
export function requestTerminalPanel(): void {
  terminalPanelWanted.value = true
}
