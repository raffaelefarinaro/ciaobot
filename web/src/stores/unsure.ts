import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import { api } from '../lib/api'
import type { UnsureItem, UnsureList, UnsureOp } from '../lib/unsureList'
import { useTaskStore } from './tasks'
import { CARE_SCHEDULE_PREFIX } from '../lib/automationWorkspace'


/**
 * The active workspace's Unsure list (`GET /api/memory/unsure`), shared by
 * Memory · Unsure, its rail and the sidebar's counts.
 *
 * A row's action is `POST /api/memory/unsure` naming the line by its hash; the
 * answer is the list as written, so the counts move with the click. A 409 means
 * the file changed under the page (a care run, an edit in the file): the list is
 * read again and the person is told; any other refusal shows the engine's own
 * words. The list is re-read when the page opens and when the tab comes back. The care schedule's own row moving
 * (dispatched, finished) re-reads it too, since a run applies and removes lines.
 */
export const useUnsureStore = defineStore('unsure', () => {
  const tasks = useTaskStore()
  const list = ref<UnsureList | null>(null)
  const workspace = ref('')
  /** The first read for `workspace` has answered or failed. */
  const settled = ref(false)
  const loading = ref(false)
  const error = ref('')
  /** Ids of the lines with a write in flight. */
  const busy = ref<Set<string>>(new Set())
  let seq = 0

  async function load(ws: string): Promise<void> {
    const mine = ++seq
    loading.value = true
    try {
      const body = await api.get<UnsureList>(`/api/memory/unsure?workspace=${encodeURIComponent(ws)}`)
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

  /**
   * Tick, untick or remove one line. Resolves '' when it was written, else a
   * sentence for the person (the list has already been read again on a 409).
   */
  async function act(op: UnsureOp, item: UnsureItem): Promise<string> {
    const ws = workspace.value
    if (!ws || busy.value.has(item.id)) return ''
    busy.value = new Set(busy.value).add(item.id)
    try {
      const body = await api.post<UnsureList>('/api/memory/unsure', { workspace: ws, op, id: item.id })
      if (ws === workspace.value) {
        // A write answers the list as written; any read still in flight is older.
        seq += 1
        list.value = body
        settled.value = true
        loading.value = false
        error.value = ''
      }
      return ''
    } catch (cause) {
      const code = ((cause as { payload?: { code?: string } }).payload ?? {}).code
      if (code === 'line_changed') {
        await load(ws)
        return 'The list changed since this page read it, so nothing was changed. It has been read again.'
      }
      // Every other refusal (care running, busy, not a plain file) says why in
      // the engine's own words.
      return cause instanceof Error ? cause.message : String(cause)
    } finally {
      const next = new Set(busy.value)
      next.delete(item.id)
      busy.value = next
    }
  }

  // Coming back to the app re-reads the list: a care run or an edit in the file
  // may have changed it while the tab was hidden.
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

  return { list, workspace, settled, loading, error, busy, show, refresh, act }
})
