import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { api } from '../lib/api'
import { taskApiErrorMessage, taskDetailFrom } from '../lib/taskBoard'
import type { TaskDetail } from '../lib/types'
import { useProjectStore } from './projects'
import { useTaskSignalsStore } from './taskSignals'

/**
 * What a peek shows. A union, so a later kind (a schedule, a chat) adds a member
 * and its own read, not a second overlay.
 */
export type PeekTarget = { kind: 'task'; id: string; workspace: string }

/**
 * The read-only peek overlay that a link opens over the current view.
 *
 * Holds one target at a time. The read is for the workspace on screen when the
 * peek opened, and it is not the board store's: that store drops answers for a
 * workspace it has not loaded, and its description slot belongs to the editor.
 *
 * A newer open or a close makes an answer in flight stale, so it is dropped.
 * A workspace switch closes the peek, and a live re-read of the workspace's
 * tasks refreshes an open task in place.
 */
export const useItemPeekStore = defineStore('itemPeek', () => {
  const projectStore = useProjectStore()
  const taskSignals = useTaskSignalsStore()

  const target = ref<PeekTarget | null>(null)
  const task = ref<TaskDetail | null>(null)
  const loading = ref(false)
  const notFound = ref(false)
  const error = ref('')
  const isOpen = computed(() => target.value !== null)

  /** Bumped by every open, refresh and close: an answer for an older one is stale. */
  let seq = 0

  async function load(keepShown: boolean): Promise<void> {
    const current = target.value
    if (!current) return
    const mine = ++seq
    // A read with nothing on screen (an open, or a Retry from an error) shows
    // the loading state; a background refresh keeps the task it is updating.
    if (!keepShown || !task.value) loading.value = true
    notFound.value = false
    error.value = ''
    try {
      const data = await api.get<{ task?: unknown }>(
        `/api/tasks/${encodeURIComponent(current.id)}?workspace=${encodeURIComponent(current.workspace)}`,
      )
      if (mine !== seq) return
      task.value = taskDetailFrom(data?.task)
    } catch (e) {
      if (mine !== seq) return
      if ((e as { status?: number } | null)?.status === 404) {
        task.value = null
        notFound.value = true
      } else if (keepShown && task.value) {
        // A failed background refresh leaves the task on screen as it was.
      } else {
        error.value = taskApiErrorMessage(e, 'Could not read the task')
      }
    } finally {
      if (mine === seq) loading.value = false
    }
  }

  function openTask(id: string): void {
    seq++
    target.value = { kind: 'task', id, workspace: projectStore.activeWorkspace }
    task.value = null
    void load(false)
  }

  /** Re-read the open target, keeping what it shows until the answer lands. */
  function refresh(): Promise<void> {
    return load(true)
  }

  function close(): void {
    seq++
    target.value = null
    task.value = null
    loading.value = false
    notFound.value = false
    error.value = ''
  }

  watch(() => projectStore.activeWorkspace, () => close())
  watch(() => taskSignals.tasks, () => {
    if (target.value && !loading.value) void refresh()
  })

  return {
    target,
    task,
    loading,
    notFound,
    error,
    isOpen,
    openTask,
    refresh,
    close,
  }
})
