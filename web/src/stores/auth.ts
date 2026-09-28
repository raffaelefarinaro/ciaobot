import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type { ActionResult } from '../lib/types'

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false)

  async function login(token: string) {
    // One origin per browser: the session cookie this call sets is the whole
    // result, so the app is a reload away from signed-in.
    await api.post<ActionResult>('/api/auth', { token })
    authenticated.value = true
    window.location.replace('/')
  }

  async function logout() {
    try {
      await api.post('/api/auth/logout')
    } catch {
      /* still clear local auth state */
    }
    authenticated.value = false
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
