import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api } from '../lib/api'
import type { ChangesFeed } from '../lib/changesFeed'

/**
 * The active workspace's Changes feed (`GET /api/memory/changes`), shared by
 * Memory · Changes, its This week window and the care card's summary line. Read-only: Undo opens a chat, it writes nothing here.
 * A surface that opens re-reads it, since a care run or a memory pass may have
 * written since.
 */
export const useChangesStore = defineStore('changes', () => {
  const feed = ref<ChangesFeed | null>(null)
  const workspace = ref('')
  /** The first read for `workspace` has answered or failed. */
  const settled = ref(false)
  const error = ref('')
  let seq = 0

  async function load(ws: string): Promise<void> {
    const mine = ++seq
    try {
      const body = await api.get<ChangesFeed>(`/api/memory/changes?workspace=${encodeURIComponent(ws)}`)
      if (mine !== seq || ws !== workspace.value) return
      feed.value = body
      error.value = ''
    } catch (cause) {
      if (mine !== seq || ws !== workspace.value) return
      error.value = cause instanceof Error ? cause.message : String(cause)
    } finally {
      if (mine === seq && ws === workspace.value) settled.value = true
    }
  }

  /** Show `ws`'s feed: a new workspace starts from nothing, the same one is kept. */
  function show(ws: string): void {
    if (!ws || ws === workspace.value) return
    workspace.value = ws
    feed.value = null
    settled.value = false
    error.value = ''
    void load(ws)
  }

  async function refresh(): Promise<void> {
    if (workspace.value) await load(workspace.value)
  }

  return { feed, workspace, settled, error, show, refresh }
})
