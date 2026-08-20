import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type {
  ProposalsResponse,
  ProposalRow,
  ProposalBatchResponse,
  ProposalDismissOlderResponse,
} from '../lib/types'

/**
 * Proposal-review queue. Mirrors the housekeeping store's best-effort
 * contract: a failed fetch leaves the list empty rather than throwing. The
 * batch endpoints do not return the updated list (they return per-row
 * results), so after any mutation the store re-fetches to reflect what the
 * server actually kept.
 */
export const useProposalsStore = defineStore('proposals', () => {
  const rows = ref<ProposalRow[]>([])
  const loading = ref(false)
  const busy = ref(false)
  const error = ref('')

  async function fetch() {
    loading.value = true
    error.value = ''
    try {
      const data = await api.get<ProposalsResponse>('/api/proposals')
      rows.value = data.rows ?? []
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Could not load proposals'
    } finally {
      loading.value = false
    }
  }

  async function act(id: string, action: 'accept' | 'dismiss') {
    busy.value = true
    error.value = ''
    try {
      await api.post<ProposalBatchResponse>(`/api/proposals/${id}/${action}`)
      await fetch()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Action failed'
    } finally {
      busy.value = false
    }
  }

  async function batch(ids: string[], action: 'accept' | 'dismiss') {
    if (!ids.length) return
    busy.value = true
    error.value = ''
    try {
      await api.post<ProposalBatchResponse>('/api/proposals/batch', { action, ids })
      await fetch()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Batch action failed'
    } finally {
      busy.value = false
    }
  }

  async function dismissOlderThan(date: string) {
    busy.value = true
    error.value = ''
    try {
      await api.post<ProposalDismissOlderResponse>(
        `/api/proposals/dismiss-older-than?date=${encodeURIComponent(date)}`,
      )
      await fetch()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Dismiss failed'
    } finally {
      busy.value = false
    }
  }

  return { rows, loading, busy, error, fetch, act, batch, dismissOlderThan }
})
