/** Shared restart-drain helpers for the PWA. */

export const RESTART_DRAIN_MESSAGE =
  'Ciaobot is waiting for active chats to finish before restarting'

export const DEFAULT_RESTART_MESSAGE =
  'Ciaobot is restarting… Waiting for active chats to finish.'

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
  while (true) {
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
