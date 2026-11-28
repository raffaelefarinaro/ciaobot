<template>
  <ul class="mus" role="list">
    <li v-for="source in sources" :key="key(source)">
      <RouterLink
        v-if="source.kind === 'chat'"
        class="mus-chip"
        :to="`/chat/${source.id}`"
        :aria-label="`Open the chat ${source.id}`"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
        {{ source.id }}
      </RouterLink>
      <button
        v-else-if="source.kind === 'note' && fileOf(source.path)"
        type="button"
        class="mus-chip"
        :aria-label="`Open the note ${source.path}`"
        @click="open(source.path)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v14H7z" /><path d="M14 3v4h4" /></svg>
        {{ source.path }}
      </button>
      <span v-else class="mus-text">{{ source.kind === 'note' ? source.path : source.label }}</span>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useFileViewerStore } from '../stores/fileViewer'
import { vaultFile, type UnsureSource } from '../lib/unsureList'

/** Where an Unsure item came from: chats as links to the chat, vault notes opening in the viewer, other words plain. */
const props = defineProps<{ sources: UnsureSource[]; listPath: string }>()
const fileViewer = useFileViewerStore()

function key(source: UnsureSource): string {
  return source.kind === 'chat' ? source.id : source.kind === 'note' ? source.path : source.label
}
function fileOf(path: string): string {
  return vaultFile(props.listPath, path)
}
function open(path: string): void {
  const file = fileOf(path)
  if (file) void fileViewer.open(file)
}
</script>

<style scoped>
.mus { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.mus li { display: flex; min-width: 0; max-width: 100%; }
.mus-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
  text-decoration: none;
  overflow-wrap: anywhere;
  text-align: start;
  transition: background 120ms var(--ease), color 120ms var(--ease);
}
.mus-chip svg { flex: none; }
.mus-chip:hover { background: var(--bg3); color: var(--fg); }
.mus-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.mus-text { display: inline-flex; align-items: center; min-height: 36px; color: var(--fg3); font-size: var(--text-sm); overflow-wrap: anywhere; }
@media (pointer: coarse) { .mus-chip, .mus-text { min-height: var(--touch); } }
@media (prefers-reduced-motion: reduce) { .mus-chip { transition: none; } }
</style>
