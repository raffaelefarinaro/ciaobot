/** Shared restart-drain helpers for the PWA. */

export const RESTART_DRAIN_MESSAGE =
  'Ciaobot is waiting for active chats to finish before restarting'

export const DEFAULT_RESTART_MESSAGE =
  'Ciaobot is restarting… Waiting for active chats to finish.'

/**
 * Longest restart message the notice card will show. The card is ~400px wide,
 * so this is about three lines — enough for every message we send, and a bound
 * on the ones we don't control.
 */
export const MAX_RESTART_MESSAGE_CHARS = 200

/**
 * Normalise a restart message for display.
 *
 * Most callers pass a fixed literal, but a drain rejection can arrive as an
 * arbitrary server/host error string that merely contains the drain phrase, so
 * this collapses whitespace (a multi-line traceback would otherwise stack up
 * the card) and truncates rather than trusting the length.
 */
export function restartMessageForDisplay(message: string | undefined | null): string {
  const collapsed = (message || '').replace(/\s+/g, ' ').trim()
  if (!collapsed) return DEFAULT_RESTART_MESSAGE
  if (collapsed.length <= MAX_RESTART_MESSAGE_CHARS) return collapsed
  return `${collapsed.slice(0, MAX_RESTART_MESSAGE_CHARS - 1).trimEnd()}…`
}

export function isRestartDrainMessage(message: string | undefined | null): boolean {
  if (!message) return false
  return message.includes('waiting for active chats to finish before restarting')
}

/**
 * Poll until the server goes down and comes back ready, then reload.
 * Same signal App.vue's boot overlay uses (`/api/startup-status`).
 */
export async function reloadWhenServerReady(timeoutMs = 120000): Promise<void> {
  const start = Date.now()
  let sawDown = false
  // for(;;) rather than while(true): same loop, and no-constant-condition
  // exempts it. The exits are the timeout check and the ready reload below.
  for (;;) {
    try {
      const res = await fetch('/api/startup-status')
      if (res.ok) {
        const data = await res.json()
        if (!data.overall_ready) {
          sawDown = true
        } else if (sawDown) {
          location.reload()
          return
        }
      } else {
        sawDown = true
      }
    } catch {
      sawDown = true
    }
    if (Date.now() - start > timeoutMs) {
      location.reload()
      return
    }
    await new Promise(r => setTimeout(r, 1000))
  }
}
