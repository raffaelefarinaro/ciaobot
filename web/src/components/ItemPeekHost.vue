<script setup lang="ts">
/**
 * Where an item opened from a link is drawn. Mounted once in ChatLayout, beside
 * the file viewer, and shows whichever target the store holds.
 *
 * A task link opens the board's own editor over the view it was clicked on, in
 * `TaskWorkspaceSurface`'s overlay mode: no address change, no board chrome, and
 * the surface stays mounted across its internal closes (the delegation preview)
 * so the host only unmounts on the surface's final `close`.
 *
 * The host owns the three exits — the surface's own close, a route change and a
 * workspace switch — and every one of them awaits `requestClose()`. A refusal
 * (a blank title, a refused or incomplete save, a busy nested sheet) cancels
 * the action and leaves the route, the workspace and the draft as they were.
 * A kind added to `PeekTarget` gets its own branch here.
 */
import { computed, inject, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { routerKey } from 'vue-router'
import TaskWorkspaceSurface from './TaskWorkspaceSurface.vue'
import { useItemPeekStore } from '../stores/itemPeek'
import { useProjectStore } from '../stores/projects'

const peek = useItemPeekStore()
const projectStore = useProjectStore()
const router = inject(routerKey, null)

/** The mounted surface, `null` between targets. */
const surface = ref<InstanceType<typeof TaskWorkspaceSurface> | null>(null)
const visible = computed(() => peek.target?.kind === 'task')
const targetId = computed(() => (peek.target?.kind === 'task' ? peek.target.id : ''))

/** Ask the surface to finish; with no surface there is nothing to refuse. */
async function requestSurfaceClose(): Promise<boolean> {
  const current = surface.value
  if (!current) return true
  return current.requestClose()
}

// ── Focus ─────────────────────────────────────────────────────────────────
//
// The surface's dialog restores focus to its own opener, but this host may
// unmount it on the same tick as `close`, before that watcher runs. So the
// element that had focus when the target opened is remembered here, and given
// it back once the overlay is gone.

let opener: HTMLElement | null = null

/** The overlay took the focus the user left on the page, and has now given it up. */
function focusWasLost(): boolean {
  const focused = document.activeElement
  if (!focused || focused === document.body) return true
  // The element still holds focus only in the tick its own sheet was removed.
  return !focused.isConnected
}

function restoreOpener(): void {
  const target = opener
  opener = null
  if (!target?.isConnected) return
  // A dialog opened while the overlay was up owns focus now: do not pull the
  // user out of it on the way out of ours.
  const focused = document.activeElement
  if (focused instanceof HTMLElement && focused.closest('[role="dialog"]')) return
  if (!focusWasLost()) return
  target.focus()
}

watch(() => peek.isOpen, (open) => {
  // Recorded once per open, and only a real element: with focus already on the
  // body there is nothing to give back. A replacement target keeps the opener
  // the user actually came from, and {@link restoreOpener} consumes it.
  if (!open) return
  const focused = document.activeElement
  opener = focused instanceof HTMLElement && focused !== document.body ? focused : null
}, { immediate: true })

/** The surface finished on its own terms: the target is done, and the host unmounts it. */
function onClosed(): void {
  peek.close()
  void nextTick(restoreOpener)
}

// ── Route guard ───────────────────────────────────────────────────────────
//
// A route change would take the screen away from under an overlay that opened
// over it. Registered for the host's lifetime (not per target) so the guard is
// in place before any navigation begins, and removed with the host.

const unregisterRoute = router?.beforeEach(async () => requestSurfaceClose()) ?? null

// ── Workspace guard ───────────────────────────────────────────────────────
//
// The settings editor owns the store's single guard slot, so this registers in
// the registry that runs after it. Registered while a target is open: with no
// overlay there is no draft to protect.

let unregisterWorkspace: (() => void) | null = null

function registerWorkspaceGuard(): void {
  if (unregisterWorkspace) return
  unregisterWorkspace = projectStore.registerWorkspaceSwitchGuard(async () => requestSurfaceClose())
}

function unregisterWorkspaceGuard(): void {
  unregisterWorkspace?.()
  unregisterWorkspace = null
}

watch(() => peek.isOpen, (open) => {
  if (open) registerWorkspaceGuard()
  else unregisterWorkspaceGuard()
}, { immediate: true })

onBeforeUnmount(() => {
  unregisterRoute?.()
  unregisterWorkspaceGuard()
})
</script>

<template>
  <TaskWorkspaceSurface
    v-if="visible"
    ref="surface"
    mode="overlay"
    :task-id="targetId"
    @close="onClosed"
  />
</template>
