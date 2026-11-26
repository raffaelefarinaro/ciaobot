import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '../lib/api'
import { apiErrorMessage } from '../lib/errorMessage'
import { checkingHealth, failedHealth, readyHealth, type SourceHealth } from '../lib/sourceHealth'
import type {
  HousekeepingDismissResponse,
  HousekeepingResponse,
  HousekeepingRunResponse,
  OperatorAction,
  WorkspaceHealthCheck,
  WorkspaceHealthResponse,
} from '../lib/types'

const REFRESH_INTERVAL_MS = 60_000
/** How long Home waits for the first load before painting what it has. `api`
 *  has no fetch timeout, so one stalled read must not hide blocking and error
 *  rows (and their Fix button); rows still arrive when the reads land. */
export const INITIAL_LOAD_GATE_MS = 3000

/**
 * Home-screen operator-action strip. Mirrors `checkPackageStatus`'s best-effort
 * contract: a failure leaves the strip empty rather than showing an error.
 * Refreshes on first access, on a short interval, and on window focus, so a
 * cleared action (run in chat) disappears without a manual reload. `run(id)`
 * performs one action's work and re-renders from the response's fresh list.
 */
export const useHousekeepingStore = defineStore('housekeeping', () => {
  const actions = ref<OperatorAction[]>([])
  /** Whether `/api/housekeeping` could be read. Installation-wide, so there is
   *  no scope change to reset it on. */
  const actionsHealth = ref<SourceHealth>(checkingHealth())
  /** Which actions answer is current: `refresh` takes a number before it asks,
   *  and `run`/`dismiss` bump it when their own fresh list lands, so a poll
   *  opened before the decision cannot restore the pre-decision list. */
  let actionsSeq = 0
  const loading = ref(false)
  /** True from `init` until its first two reads (actions, workspace
   *  health) have each answered or failed. Home paints no "Needs you"
   *  row before then, so rows do not arrive one source at a time and push the
   *  composer down in steps. Later refreshes never set it. */
  const initialLoadPending = ref(false)

  // ── Workspace health ─────────────────────────────────────────────────────
  //
  // The same install-wide report Settings → Advanced shows, read here so an
  // error (a vault root that is missing, a vault that is not writable) raises a
  // row on Home instead of waiting in a tab nobody opens. Best-effort like the
  // other sources: a failed read leaves the previous report in place.
  const health = ref<WorkspaceHealthResponse | null>(null)
  /** The checks that are errors. Warnings stay in Settings: Home is for what
   *  stops something working. */
  const healthErrors = computed<WorkspaceHealthCheck[]>(
    () => (health.value?.checks ?? []).filter(check => check.status === 'error'),
  )
  const healthFixing = ref(false)
  const healthFixError = ref('')
  const runningIds = ref<Set<string>>(new Set())
  let initialized = false

  async function refresh() {
    const seq = ++actionsSeq
    try {
      const data = await api.get<HousekeepingResponse>('/api/housekeeping')
      if (seq !== actionsSeq) return
      actions.value = data.actions ?? []
      actionsHealth.value = readyHealth()
    } catch (e) {
      // Best-effort: the strip just stays empty if the check fails. The failure
      // is observable through `actionsHealth`; the rows are left as they were.
      if (seq !== actionsSeq) return
      actionsHealth.value = failedHealth(actionsHealth.value, e)
    }
  }

  async function refreshHealth(): Promise<void> {
    try {
      health.value = await api.get<WorkspaceHealthResponse>('/api/workspace-health')
    } catch {
      // Best-effort: the rows stay as they were.
    }
  }

  /** Apply the automatic remedies (`POST /api/workspace-health/fix`), the same
   *  call Settings' "Fix issues" makes, and adopt the fresh report it returns. */
  async function fixHealth(): Promise<boolean> {
    healthFixing.value = true
    healthFixError.value = ''
    try {
      const data = await api.post<WorkspaceHealthResponse>('/api/workspace-health/fix', {})
      if (Array.isArray(data?.checks)) health.value = data
      else await refreshHealth()
      return true
    } catch (e) {
      healthFixError.value = apiErrorMessage(e, 'Fix failed')
      await refreshHealth()
      return false
    } finally {
      healthFixing.value = false
    }
  }

  async function run(id: string): Promise<{ ok: boolean; summary: string }> {
    runningIds.value = new Set(runningIds.value).add(id)
    try {
      const data = await api.post<HousekeepingRunResponse>(`/api/housekeeping/${id}/run`)
      actionsSeq += 1
      actions.value = data.actions ?? []
      actionsHealth.value = readyHealth()
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
      actionsSeq += 1
      actions.value = data.actions ?? []
      actionsHealth.value = readyHealth()
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
    initialLoadPending.value = true
    void Promise.race([
      Promise.allSettled([refresh(), refreshHealth()]),
      new Promise<void>(resolve => setTimeout(resolve, INITIAL_LOAD_GATE_MS)),
    ]).then(() => { initialLoadPending.value = false })
    if (typeof window !== 'undefined') {
      window.addEventListener('focus', () => {
        void refresh()
        void refreshHealth()
      })
      window.setInterval(() => {
        void refresh()
        void refreshHealth()
      }, REFRESH_INTERVAL_MS)
    }
  }

  return {
    actions,
    actionsHealth,
    loading,
    initialLoadPending,
    health,
    healthErrors,
    healthFixing,
    healthFixError,
    refreshHealth,
    fixHealth,
    runningIds,
    refresh,
    run,
    dismiss,
    init,
  }
})
