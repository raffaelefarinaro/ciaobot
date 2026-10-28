<script setup lang="ts">
/**
 * The `/tasks` route's task board.
 *
 * The board, the full task editor, resolution and delegation state all live in
 * `TaskWorkspaceSurface.vue`; this wrapper is the route entry point. It forwards
 * the pane's `open-sidebar` request and adds no behavior of its own, so another
 * caller can render the same surface later without a second owner of that state.
 */
import TaskWorkspaceSurface from './TaskWorkspaceSurface.vue'

const emit = defineEmits<{ 'open-sidebar': [] }>()
</script>

<template>
  <TaskWorkspaceSurface @open-sidebar="emit('open-sidebar')" />
</template>
