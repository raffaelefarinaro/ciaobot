import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type { ActionResult } from '../lib/types'
import { clearSharedContent } from '../lib/sharedContent'

/**
 * A post-login destination, or null when it is not this app's internal path.
 * Control characters are rejected before parsing because a raw tab makes the
 * browser normalize `/<tab>/host` into `https://host/`.
 */
export function safeReturnPath(requested: string | null | undefined): string | null {
  if (!requested) return null
  if (/[\u0000-\u001f\u007f]/.test(requested)) return null
  if (!requested.startsWith('/') || requested.startsWith('//') || requested.includes('\\')) return null
  if (/^\/login(?:[/?#]|$)/.test(requested)) return null
  if (/[?&](?:setup_token|token)=/i.test(requested)) return null
  try {
    const resolved = new URL(requested, window.location.origin)
    if (resolved.origin !== window.location.origin) return null
  } catch {
    return null
  }
  return requested
}

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false)

  async function login(token: string) {
    // One origin per browser: the session cookie this call sets is the whole
    // result, so the app is a reload away from signed-in.
    await api.post<ActionResult>('/api/auth', { token })
    authenticated.value = true
    const params = new URLSearchParams(window.location.search)
    const shared = params.get('shared')
    const safe = safeReturnPath(params.get('returnTo'))
    window.location.replace(safe ?? (shared ? `/?shared=${encodeURIComponent(shared)}` : '/'))
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } catch {
      /* still clear local auth state */
    }
    authenticated.value = false
    await clearSharedContent().catch(() => {})
    window.location.assign('/login')
  }

  async function check() {
    try {
      // Use raw fetch so a 401 here never triggers api.ts's /login redirect,
      // which would reload the tab this probe is trying to answer for.
      const res = await fetch('/api/auth/check', { credentials: 'same-origin', redirect: 'manual' })
      authenticated.value = res.ok
    } catch {
      authenticated.value = false
    }
  }

  return { authenticated, login, logout, check }
})
