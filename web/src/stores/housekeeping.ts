import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type {
  HousekeepingDismissResponse,
  HousekeepingResponse,
  HousekeepingRunResponse,
  OperatorAction,
} from '../lib/types'

const REFRESH_INTERVAL_MS = 60_000

/**
 * Home-screen operator-action strip. Mirrors `checkPackageStatus`'s best-effort
 * contract: a failure leaves the strip empty rather than showing an error.
 * Refreshes on first access, on a short interval, and on window focus, so a
 * cleared action (run in chat) disappears without a manual reload. `run(id)`
 * performs one action's work and re-renders from the response's fresh list.
 */
export const useHousekeepingStore = defineStore('housekeeping', () => {
  const actions = ref<OperatorAction[]>([])
  const loading = ref(false)
  const runningIds = ref<Set<string>>(new Set())
  let initialized = false

  async function refresh() {
    try {
      const data = await api.get<HousekeepingResponse>('/api/housekeeping')
      actions.value = data.actions ?? []
    } catch {
      // Best-effort: the strip just stays empty if the check fails.
    }
  }

  async function run(id: string): Promise<{ ok: boolean; summary: string }> {
    runningIds.value = new Set(runningIds.value).add(id)
    try {
      const data = await api.post<HousekeepingRunResponse>(`/api/housekeeping/${id}/run`)
      actions.value = data.actions ?? []
      if (!data.ok) {
        return { ok: false, summary: data.error || 'Run failed' }
      }
      return { ok: true, summary: data.summary || '' }
    } catch (e) {
      // Best-effort like refresh: re-fetch so the strip reflects whatever the
      // server actually did even if the run response itself was lost.
      await refresh()
      return { ok: false, summary: e instanceof Error ? e.message : 'Run failed' }
    } finally {
      const next = new Set(runningIds.value)
      next.delete(id)
      runningIds.value = next
    }
  }

  async function dismiss(id: string): Promise<{ ok: boolean; summary: string }> {
    try {
      const data = await api.post<HousekeepingDismissResponse>(`/api/housekeeping/${id}/dismiss`)
      actions.value = data.actions ?? []
      return { ok: !!data.ok, summary: data.summary || '' }
    } catch (e) {
      // Best-effort like refresh: re-fetch so the strip reflects whatever the
      // server actually did even if the dismiss response itself was lost.
      await refresh()
      return { ok: false, summary: e instanceof Error ? e.message : 'Dismiss failed' }
    }
  }

  function init() {
    if (initialized) return
    initialized = true
    void refresh()
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', () => void refresh())
      window.setInterval(refresh, REFRESH_INTERVAL_MS)
    }
  }

  return { actions, loading, runningIds, refresh, run, dismiss, init }
})
