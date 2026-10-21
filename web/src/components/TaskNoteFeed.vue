<script setup lang="ts">
/**
 * A task's progress notes, newest first: each a bubble that names who wrote it
 * and when, with its links live. Shared by the board's editor and the chat-side
 * task peek.
 *
 * `actions` renders in the note's meta line (the board's Edit button). `editor`
 * replaces the note text for the note whose id is `editingId`. Both are scoped
 * slots, so the owner keeps its own state and handlers.
 */
import { formatCompletedAt } from '../lib/taskBoard'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import type { TaskUpdate } from '../lib/types'

defineProps<{
  updates: TaskUpdate[]
  editingId?: string
}>()
</script>

<template>
  <ol class="task-notes" aria-label="Progress notes, newest first">
    <li
      v-for="item in updates"
      :key="item.id"
      class="task-note"
      :data-actor="item.actor === 'agent' ? 'agent' : 'user'"
    >
      <div class="task-note-meta">
        <span class="task-note-author">{{ item.actor === 'agent' ? 'Agent' : 'You' }}</span>
        <span class="task-note-when">{{ formatCompletedAt(item.recorded_at) || 'No time recorded' }}<template v-if="item.edited_at"> · edited</template></span>
        <slot name="actions" :note="item" />
      </div>
      <template v-if="editingId === item.id">
        <slot name="editor" :note="item" />
      </template>
      <!-- eslint-disable-next-line vue/no-v-html — rendered via DOMPurify -->
      <div v-else class="task-note-text markdown" v-html="renderUserMarkdown(item.text)"></div>
    </li>
  </ol>
</template>

<style scoped>
/* One note, drawn as a comment: a tonal bubble, the author leading its line. The
   agent's notes take the agent's violet so who said what reads at a glance;
   the text says it too, so colour is never the only cue. */
.task-notes {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin: 0;
  padding: 0;
  list-style: none;
}
.task-note {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-2) var(--space-3) var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 45%, var(--bg2));
}
.task-note[data-actor='agent'] {
  border-color: color-mix(in srgb, var(--accent2) 35%, var(--border));
  background: color-mix(in srgb, var(--accent2) 8%, var(--bg2));
}
.task-note-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 28px;
}
.task-note-author { color: var(--fg); font-size: var(--text-sm); font-weight: 600; }
.task-note-when { color: var(--fg3); font-size: var(--text-xs); }
/* The owner's action button sits in the meta line, so its reveal and touch
   sizing are styled from here through :deep. */
.task-note :deep(.task-note-edit) {
  margin-left: auto;
  min-height: 26px;
  padding-block: 0;
  border-color: transparent;
  background: none;
  color: var(--fg3);
  opacity: 0;
  transition: opacity 120ms var(--ease);
}
.task-note:hover :deep(.task-note-edit),
.task-note:focus-within :deep(.task-note-edit),
.task-note :deep(.task-note-edit:focus-visible) { opacity: 1; }
.task-note :deep(.task-note-edit:hover:not(:disabled)) { color: var(--fg); border-color: var(--border); }
@media (hover: none) { .task-note :deep(.task-note-edit) { opacity: 1; } }
.task-note-text {
  color: var(--fg);
  font-size: var(--text-sm);
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.task-note-text :deep(p),
.task-note-text :deep(ul),
.task-note-text :deep(ol) { margin: 0 0 0.5em; }
.task-note-text :deep(ul),
.task-note-text :deep(ol) { padding-left: 1.4em; }
.task-note-text :deep(:last-child) { margin-bottom: 0; }
.task-note-text :deep(a) { color: var(--accent); text-underline-offset: 2px; }
@media (pointer: coarse) {
  .task-note :deep(.task-note-edit) { min-height: var(--touch); }
}
</style>
