import { ref } from 'vue'
import { api } from '../lib/api'

export interface WelcomeStatus {
  pending: boolean
  workspace_root: string
  provider: string
  provider_label: string
  on_host: boolean
  platform: string
}

const NOT_PENDING: WelcomeStatus = {
  pending: false,
  workspace_root: '',
  provider: '',
  provider_label: '',
  on_host: false,
  platform: '',
}

// Shared by every placement: one read per page load, whoever asks first. The
// password is deliberately not kept here: callers hold it only while shown.
const status = ref<WelcomeStatus | null>(null) // null = not loaded yet
let inflight: Promise<void> | null = null

async function load(): Promise<void> {
  try {
    const res = await api.get<Partial<WelcomeStatus>>('/api/welcome')
    status.value = res && typeof res === 'object' && res.pending === true
      ? { ...NOT_PENDING, ...res, pending: true }
      : { ...NOT_PENDING }
  } catch {
    status.value = { ...NOT_PENDING }
  }
}

export function useWelcome() {
  function ensureStatus(): Promise<void> {
    if (status.value) return Promise.resolve()
    if (!inflight) inflight = load()
    return inflight
  }

  async function fetchPassword(): Promise<string> {
    const res = await api.get<{ password?: unknown }>('/api/welcome/password')
    return typeof res?.password === 'string' ? res.password : ''
  }

  async function openFolder(): Promise<void> {
    await api.post('/api/welcome/open-folder', {})
  }

  async function dismiss(): Promise<void> {
    status.value = { ...NOT_PENDING }
    try {
      await api.post('/api/welcome/dismiss', {})
    } catch { /* the next reload shows the card again */ }
  }

  return { status, ensureStatus, fetchPassword, openFolder, dismiss }
}

export function resetWelcomeForTests(): void {
  status.value = null
  inflight = null
}
