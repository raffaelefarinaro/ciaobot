import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'

export const useAuthStore = defineStore('auth', () => {
  const authenticated = ref(false)

  async function login(token: string) {
    await api.post('/api/auth', { token })
    authenticated.value = true
    const { router } = await import('../router')
    router.push('/')
  }

  async function logout() {
    await api.post('/api/auth/logout')
    authenticated.value = false
    const { router } = await import('../router')
    router.push('/login')
  }

  async function check() {
    try {
      await api.get('/api/auth/check')
      authenticated.value = true
    } catch {
      authenticated.value = false
    }
  }

  return { authenticated, login, logout, check }
})
