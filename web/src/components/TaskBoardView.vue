<script setup lang="ts">
/**
 * The `/tasks` route's task board.
 *
 * The board, the full task editor, resolution and delegation state all live in
 * `TaskWorkspaceSurface.vue`; this wrapper is the route entry point. It forwards
 * the pane's `open-sidebar` request and the open task's pull-to-close, and hands
 * ChatLayout the surface's previous / next task, close and New task, adding no
 * behavior of its own, so another caller can render the same surface later
 * without a second owner of that state.
 */
import { ref } from 'vue'
import TaskWorkspaceSurface from './TaskWorkspaceSurface.vue'

const emit = defineEmits<{
  'open-sidebar': []
  pull: [dy: number]
  'pull-end': [dy: number, velocity: number]
}>()

const surface = ref<InstanceType<typeof TaskWorkspaceSurface> | null>(null)

defineExpose({
  selectAdjacent: (direction: -1 | 1): boolean => surface.value?.selectAdjacent(direction) ?? false,
  closeDetailPage: (): Promise<void> => surface.value?.closeDetailPage() ?? Promise.resolve(),
  openCreate: (): void => surface.value?.openCreate(),
})
</script>

<template>
  <TaskWorkspaceSurface
    ref="surface"
    @open-sidebar="emit('open-sidebar')"
    @pull="emit('pull', $event)"
    @pull-end="(dy: number, v: number) => emit('pull-end', dy, v)"
  />
</template>
