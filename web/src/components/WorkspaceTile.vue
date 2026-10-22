<template>
  <span class="workspace-tile" :data-workspace-color="accent" aria-hidden="true">{{ key || initial }}</span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useProjectStore } from '../stores/projects'
import { workspaceAccent } from '../lib/workspaceColors'
import { workspaceLabel } from '../lib/workspaceLabel'

// A workspace's identity tile on the settings pages: its 1-9 shortcut key (the
// same rule as the sidebar) or, past nine, its initial, tinted in its accent.
const props = defineProps<{ name: string }>()
const projectStore = useProjectStore()

const key = computed(() => {
  const index = projectStore.workspaceOptions.findIndex((workspace) => workspace.name === props.name) + 1
  return index >= 1 && index <= 9 ? String(index) : ''
})
const initial = computed(() => (workspaceLabel(props.name).trim()[0] || '·').toUpperCase())
const accent = computed(() => workspaceAccent(projectStore.workspaces, props.name, projectStore.activeWorkspace))
</script>

<style scoped>
/* A shape and a letter, so it can never be mistaken for an attention dot. */
.workspace-tile {
  display: grid;
  place-items: center;
  flex: 0 0 22px;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent);
  color: var(--accent);
  font-size: var(--text-xs);
  font-weight: 700;
  line-height: 1;
}
</style>
