// A stable id for this browser.
//
// Needed because some server state is per client rather than per user: an
// unsent-draft claim, for instance, belongs to the browser holding the draft,
// and one browser must not be able to cancel another's. A user id would be too
// coarse (every device shares it) and a per-tab id too fine (a reload would
// orphan the claim), so this sits in localStorage: one id per browser profile,
// stable across reloads and restarts.
//
// Private-mode and storage-disabled browsers fall back to a per-session id held
// in memory. Those clients still work; their claims are simply re-made under a
// new id after a reload, and the old one ages out on the server.

const STORAGE_KEY = 'ciao-client-id'

let cached: string | null = null

function randomId(): string {
  const cryptoObj = typeof crypto !== 'undefined' ? crypto : undefined
  if (cryptoObj?.randomUUID) return cryptoObj.randomUUID()
  if (cryptoObj?.getRandomValues) {
    const bytes = cryptoObj.getRandomValues(new Uint8Array(16))
    return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
  }
  return `c-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export function clientId(): string {
  if (cached) return cached
  try {
    if (typeof localStorage !== 'undefined') {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored) {
        cached = stored
        return cached
      }
      const fresh = randomId()
      localStorage.setItem(STORAGE_KEY, fresh)
      cached = fresh
      return cached
    }
  } catch {
    // Storage unavailable (private mode, disabled cookies): fall through.
  }
  cached = randomId()
  return cached
}

// Tests only: forget the memoised value so a case can control the id.
export function resetClientIdForTests(): void {
  cached = null
}
