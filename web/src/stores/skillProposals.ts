import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { api } from '../lib/api'
import { CARE_SCHEDULE_PREFIX } from '../lib/automationWorkspace'
import type { SkillProposalList } from '../lib/skillProposals'
import { useTaskStore } from './tasks'

/**
 * The active workspace's open skill-edit proposals
 * (`GET /api/memory/skill-proposals`), shared by Memory · Skills and the
 * sidebar's counts. Read-only: Approve opens a chat and Reject saves the file's
 * `status:` line (`lib/proposalDecision.ts`); the page calls `refresh` after
 * either. The list is re-read when the page opens, when the tab comes back and
 * when the care schedule's row moves, since a care run can write proposals.
 */
export const useSkillProposalsStore = defineStore('skillProposals', () => {
  const tasks = useTaskStore()
  const list = ref<SkillProposalList | null>(null)
  const workspace = ref('')
  const settled = ref(false)
  const loading = ref(false)
  const error = ref('')
  let seq = 0

  async function load(ws: string): Promise<void> {
    const mine = ++seq
    loading.value = true
    try {
      const body = await api.get<SkillProposalList>(`/api/memory/skill-proposals?workspace=${encodeURIComponent(ws)}`)
      if (mine !== seq || ws !== workspace.value) return
      list.value = body
      error.value = ''
    } catch (cause) {
      if (mine !== seq || ws !== workspace.value) return
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      if (mine === seq && ws === workspace.value) {
        settled.value = true
        loading.value = false
      }
    }
  }

  /** Show `ws`'s list: a new workspace starts from nothing, the same one is kept. */
  function show(ws: string): void {
    if (!ws || ws === workspace.value) return
    workspace.value = ws
    list.value = null
    settled.value = false
    error.value = ''
    void load(ws)
  }

  async function refresh(): Promise<void> {
    if (workspace.value) await load(workspace.value)
  }

  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void refresh()
    })
  }

  watch(
    () => {
      const row = tasks.schedules.find(s => s.schedule_id === `${CARE_SCHEDULE_PREFIX}${workspace.value}`)
      return row ? `${row.last_status ?? ''}|${row.last_dispatched_at ?? ''}` : ''
    },
    (next, previous) => { if (previous && next !== previous) void refresh() },
  )

  return { list, workspace, settled, loading, error, show, refresh }
})
