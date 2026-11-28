<template>
  <ul class="rvs" role="list">
    <li v-for="source in sources" :key="sourceKey(source)">
      <RouterLink
        v-if="source.kind === 'chat'"
        class="rvs-chip"
        :to="`/chat/${source.id}`"
        :aria-label="`Open the chat ${source.id}`"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 5h16v11H9l-5 4z" /></svg>
        {{ source.id }}
      </RouterLink>
      <button
        v-else-if="source.kind === 'note' && fileOf(source.path)"
        type="button"
        class="rvs-chip"
        :aria-label="`Open the note ${source.path}`"
        @click="open(source.path)"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 3h7l4 4v14H7z" /><path d="M14 3v4h4" /></svg>
        {{ source.path }}
      </button>
      <a
        v-else-if="source.kind === 'link'"
        class="rvs-chip"
        :href="source.url"
        target="_blank"
        rel="noopener noreferrer"
        :aria-label="`Open ${source.url} in a new tab`"
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 4h6v6" /><path d="M20 4l-9 9" /><path d="M18 14v6H4V6h6" /></svg>
        {{ source.url }}
      </a>
      <span v-else class="rvs-text">{{ sourceKey(source) }}</span>
    </li>
  </ul>
</template>

<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { useFileViewerStore } from '../stores/fileViewer'
import { resolveUnder, sourceKey, type ReviewSource } from '../lib/reviewQueue'

/**
 * Where a queue item came from (Memory · Unsure, Memory · Skills, a care
 * proposal review): chats as links to the chat, vault notes opening in the
 * viewer, a web link (already checked as http or https by the caller) in a new
 * tab, other words plain.
 */
const props = defineProps<{
  sources: ReviewSource[]
  /** The vault folder as the file viewer opens paths; a note outside it is plain text. */
  vaultRoot: string
}>()
const fileViewer = useFileViewerStore()

function fileOf(path: string): string {
  return resolveUnder(props.vaultRoot, props.vaultRoot, path)
}
function open(path: string): void {
  const file = fileOf(path)
  if (file) void fileViewer.open(file)
}
</script>

<style scoped>
.rvs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
.rvs li { display: flex; min-width: 0; max-width: 100%; }
.rvs-chip {
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
.rvs-chip svg { flex: none; }
.rvs-chip:hover { background: var(--bg3); color: var(--fg); }
.rvs-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.rvs-text { display: inline-flex; align-items: center; min-height: 36px; color: var(--fg3); font-size: var(--text-sm); overflow-wrap: anywhere; }
@media (pointer: coarse) { .rvs-chip, .rvs-text { min-height: var(--touch); } }
@media (prefers-reduced-motion: reduce) { .rvs-chip { transition: none; } }
</style>
