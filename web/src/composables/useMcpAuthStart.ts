import { computed, onBeforeUnmount, ref, shallowReactive, watch, type ComputedRef, type Ref } from 'vue'
import { chatActionErrorReason, safeExternalUrl } from '../lib/chatActions'
import type { ChatAction } from '../lib/types'
import { useProjectStore } from '../stores/projects'

interface Options {
  chatId: () => string
  provider: () => string
  server: () => string
  action?: () => ChatAction | null
  onNotice: (message: string) => void
  onError: (message: string) => void
}
interface Owner { token: object }
const leases = shallowReactive(new Map<string, Owner>())
const leaseKey = (chatId: string, provider: string, server: string) => JSON.stringify([chatId, provider, server])
export function mcpAuthBusy(chatId: string, provider: string, server: string): boolean {
  return leases.has(leaseKey(chatId, provider, server))
}
const openStates = new Set(['open', 'failed'])

export function useMcpAuthStart(options: Options): {
  busy: ComputedRef<boolean>
  opened: Ref<boolean>
  start: (actionId?: string) => Promise<void>
} {
  const store = useProjectStore()
  const opened = ref(false)
  const localBusy = ref(false)
  const contextVersion = ref(0)
  let disposed = false
  let pendingTab: Window | null = null
  const watchedId = ref('')
  let cancelled = false
  let accepted = false
  const key = () => leaseKey(options.chatId(), options.provider(), options.server())
  const busy = computed(() => localBusy.value || leases.has(key()))
  function closeTab() { if (pendingTab) { if (!pendingTab.closed) pendingTab.close(); pendingTab = null } }
  function navigate(target: unknown) {
    const url = safeExternalUrl(target)
    if (pendingTab && url && !pendingTab.closed) {
      pendingTab.opener = null
      pendingTab.location.href = url
      opened.value = true
      pendingTab = null
    }
  }
  const stopWatch = watch(() => {
    const action = (store.chatActions[options.chatId()] ?? []).find(a => a.id === watchedId.value)
    return action && action.kind === 'mcp_auth' && action.params.provider === options.provider() && action.params.server === options.server()
      ? { id: action.id, state: action.state, url: action.params.url }
      : undefined
  }, (action, previous) => {
    if (!watchedId.value) return
    if (!action) { cancelled = true; closeTab(); return }
    if (accepted && action.state === 'waiting') {
      if (safeExternalUrl(action.url)) navigate(action.url)
      else if (pendingTab && action.url) closeTab()
    }
    if (action.state === 'done' || (action.state === 'failed' && (accepted || previous?.state !== undefined))) closeTab()
  }, { flush: 'sync' })
  watch([options.chatId, options.provider, options.server], () => {
    contextVersion.value++
    closeTab()
    watchedId.value = ''
    localBusy.value = false
    opened.value = false
    options.onNotice('')
  }, { flush: 'sync' })

  async function start(actionId?: string): Promise<void> {
    const chatId = options.chatId(), provider = options.provider(), serverName = options.server()
    const version = contextVersion.value
    const lease = leaseKey(chatId, provider, serverName)
    if (disposed || !chatId || !serverName || leases.has(lease)) {
      if (leases.has(lease)) options.onNotice('Sign-in is already starting for this server.')
      return
    }
    const actions = store.chatActions[chatId] ?? []
    let action = actionId
      ? actions.find(a => a.id === actionId)
      : actions.filter(a => a.kind === 'mcp_auth' && a.params.provider === provider && a.params.server === serverName)
        .sort((a, b) => (Number.isFinite(b.created_at) ? b.created_at : 0) - (Number.isFinite(a.created_at) ? a.created_at : 0)
          || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0))[0]
    if (action && (action.kind !== 'mcp_auth' || action.params.provider !== provider || action.params.server !== serverName)) return
    if (action && !openStates.has(action.state)) return
    if (actionId && !action) {
      options.onNotice('This card changed on another device. The newer state is shown.')
      return
    }
    const owner = { token: {} }
    leases.set(lease, owner)
    localBusy.value = true
    options.onError('')
    options.onNotice('')
    opened.value = false
    cancelled = false
    accepted = false
    pendingTab = window.open('', '_blank')
    if (action) watchedId.value = action.id
    try {
      if (!action) action = await store.requestMcpAuth(chatId, serverName) ?? undefined
      if (disposed || contextVersion.value !== version) return
      if (!action) {
        closeTab()
        options.onNotice('This card changed on another device. The newer state is shown.')
        return
      }
      watchedId.value = action.id
      if (cancelled) return
      const current = (store.chatActions[chatId] ?? []).find(a => a.id === action!.id && a.kind === 'mcp_auth' && a.params.provider === provider && a.params.server === serverName)
      if (!current || !openStates.has(current.state)) { closeTab(); return }
      const next = await store.startChatAction(chatId, current.id)
      if (disposed || contextVersion.value !== version) return
      if (!next) {
        closeTab()
        options.onNotice('This card changed on another device. The newer state is shown.')
      } else if (cancelled) return
      else {
        accepted = true
        const latest = (store.chatActions[chatId] ?? []).find(a => a.id === current.id && a.kind === 'mcp_auth' && a.params.provider === provider && a.params.server === serverName)
        if (latest?.state === 'waiting') {
          if (safeExternalUrl(latest.params.url)) navigate(latest.params.url)
          else if (latest.params.url) closeTab()
        } else if (latest?.state !== 'working') closeTab()
      }
    } catch (e) {
      if (disposed || contextVersion.value !== version || cancelled) return
      closeTab()
      if (['chat_action_conflict', 'invalid_state'].includes(chatActionErrorReason(e))) {
        options.onNotice('This card changed on another device. The newer state is shown.')
      } else {
        options.onError('The sign-in could not be started. Try again.')
      }
    } finally {
      if (leases.get(lease) === owner) leases.delete(lease)
      if (!disposed && contextVersion.value === version) localBusy.value = false
    }
  }
  onBeforeUnmount(() => { disposed = true; contextVersion.value++; closeTab(); stopWatch() })
  return { busy, opened, start }
}
