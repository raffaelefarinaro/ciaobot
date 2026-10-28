import { defineStore } from 'pinia'
import { computed, ref, watch } from 'vue'
import { useProjectStore } from './projects'

/**
 * What a peek shows. A union, so a later kind (a schedule, a chat) adds a member
 * and its own surface, not a second overlay.
 */
export type PeekTarget = { kind: 'task'; id: string; workspace: string }

/**
 * The target a link opened over the current view.
 *
 * The store holds the target and nothing else. The surface that a target mounts
 * (`TaskWorkspaceSurface` in overlay mode) owns the read, the editor, resolution
 * and delegation; a read kept beside it here was a second authority that could
 * show one record while the user edited another.
 *
 * The workspace is the one on screen when the overlay opened: a switch is
 * awaited through the workspace guard first, so the target only ever names a
 * task in the workspace being drawn.
 */
export const useItemPeekStore = defineStore('itemPeek', () => {
  const projectStore = useProjectStore()

  const target = ref<PeekTarget | null>(null)
  const isOpen = computed(() => target.value !== null)

  function openTask(id: string): void {
    const current = target.value
    // A task that links to itself, or a second click on its own link, must not
    // reset the editor the user is typing in: the surface already shows it and
    // replacing the target would remount the form.
    if (current && current.kind === 'task' && current.id === id) return
    target.value = { kind: 'task', id, workspace: projectStore.activeWorkspace }
  }

  function close(): void {
    target.value = null
  }

  // Cleanup for a workspace change that bypassed the switch guard — a rename,
  // an archive, an external change. A guarded switch closes the overlay before
  // `activeWorkspace` moves; this is the fallback that never leaves a task from
  // another workspace open.
  watch(() => projectStore.activeWorkspace, () => close())

  return {
    target,
    isOpen,
    openTask,
    close,
  }
})
