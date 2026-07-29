import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false)

  async function login(token: string) {
    await api.post('/api/auth', { token })
    authenticated.value = true
    // Full reload so client-mode tunnel + stores pick up the host session.
    window.location.assign('/')
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
      // Use raw fetch so a 401 here never triggers api.ts's /login redirect
      // (that reload looped while waiting for the host password).
      const res = await fetch('/api/auth/check', { credentials: 'same-origin' })
      authenticated.value = res.ok
    } catch {
      authenticated.value = false
    }
  }

  return { authenticated, login, logout, check }
})
