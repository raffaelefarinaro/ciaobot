import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import { navigateToContent, replaceToContent } from '../lib/originNavigation'
import type { ActionResult } from '../lib/types'

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false)

  async function login(token: string) {
    const result = await api.post<ActionResult>('/api/auth', { token })
    if (window.location.hostname === '127.0.0.1' && typeof result.bridge_url !== 'string') {
      throw new Error('The client session bridge was not issued.')
    }
    authenticated.value = true
    if (typeof result.bridge_url === 'string') replaceToContent(result.bridge_url)
    else navigateToContent()
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } catch {
      /* still clear local auth state */
    }
    authenticated.value = false
    navigateToContent('/login')
  }

  async function check() {
    try {
      // Use raw fetch so a 401 here never triggers api.ts's /login redirect
      // (that reload looped while waiting for the host password).
      const res = await fetch('/api/auth/check', { credentials: 'same-origin', redirect: 'manual' })
      authenticated.value = res.ok
    } catch {
      authenticated.value = false
    }
  }

  return { authenticated, login, logout, check }
})
