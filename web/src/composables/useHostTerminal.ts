import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { isLoopbackPage } from '../lib/loopback'
import type { HostTerminalAction, HostTerminalCapabilities, HostTerminalLaunchResult } from '../lib/types'

const UNAVAILABLE: HostTerminalCapabilities = {
  available: false,
  platform: '',
  reason: 'not_on_host',
  terminal: '',
  actions: [],
}

// Shared by every placement: one read per page load, whoever asks first.
const capabilities = ref<HostTerminalCapabilities | null>(null)
let inflight: Promise<void> | null = null

async function load(): Promise<void> {
  if (!isLoopbackPage()) {
    capabilities.value = { ...UNAVAILABLE }
    return
  }
  try {
    const res = await api.get<HostTerminalCapabilities>('/api/host/terminal')
    const ok = typeof res === 'object' && res !== null && res.available === true
    capabilities.value = ok
      ? { ...UNAVAILABLE, ...res, actions: Array.isArray(res.actions) ? res.actions : [] }
      : { ...UNAVAILABLE, reason: (typeof res === 'object' && res?.reason) || 'unavailable' }
  } catch {
    capabilities.value = { ...UNAVAILABLE }
  }
}

export function useHostTerminal() {
  function ensureCapabilities(): Promise<void> {
    if (capabilities.value) return Promise.resolve()
    if (!inflight) inflight = load()
    return inflight
  }

  const available = computed(() => capabilities.value?.available === true)

  function canRun(action: HostTerminalAction): boolean {
    return available.value && (capabilities.value?.actions.includes(action) ?? false)
  }

  function runAction(action: HostTerminalAction): Promise<HostTerminalLaunchResult> {
    return api.post<HostTerminalLaunchResult>('/api/host/terminal', { action })
  }

  function openFolder(target: { chatId?: string; projectId?: string }): Promise<HostTerminalLaunchResult> {
    return api.post<HostTerminalLaunchResult>('/api/host/terminal', {
      open: 'folder',
      ...(target.chatId ? { chat_id: target.chatId } : {}),
      ...(target.projectId ? { project_id: target.projectId } : {}),
    })
  }

  return { capabilities, available, canRun, ensureCapabilities, runAction, openFolder }
}

export function resetHostTerminalForTests(): void {
  capabilities.value = null
  inflight = null
}
