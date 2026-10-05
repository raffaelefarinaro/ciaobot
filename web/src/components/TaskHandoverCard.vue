<script setup lang="ts">
/**
 * The first message of a delegated chat, drawn as what was handed over.
 *
 * The engine wrote that message for the agent (`ciao/task_attempts.py::
 * build_prompt`), so the transcript shows the parts a person reads — the task's
 * title and its description, rendered — and keeps the prompt itself, verbatim,
 * behind "Show what the agent received". ChatPanel draws this only once
 * {@link parseTaskHandover} has found both fences; anything else is an ordinary
 * message.
 */
import { computed } from 'vue'
import { renderUserMarkdown } from '../lib/safeMarkdown'
import type { TaskHandover } from '../lib/taskHandover'

const props = defineProps<{
  handover: TaskHandover
  /** The raw prompt, shown verbatim under the disclosure. */
  prompt: string
  knownPaths?: string[]
}>()

const descriptionHtml = computed(() =>
  props.handover.description ? renderUserMarkdown(props.handover.description, props.knownPaths ?? []) : '',
)
</script>

<template>
  <section class="task-handover" aria-label="Task handed over">
    <p class="task-handover-label">Task handed over</p>
    <h3 v-if="handover.title" class="task-handover-title">{{ handover.title }}</h3>
    <!-- eslint-disable-next-line vue/no-v-html -- renderUserMarkdown is the transcript's sanitising renderer -->
    <div v-if="descriptionHtml" class="task-handover-description" v-html="descriptionHtml"></div>
    <p v-else class="task-handover-empty">No description.</p>
    <details class="task-handover-raw">
      <summary>Show what the agent received</summary>
      <pre>{{ prompt }}</pre>
    </details>
  </section>
</template>

<style scoped>
.task-handover {
  min-width: 0;
  padding: 12px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg);
  text-align: left;
}
.task-handover-label {
  margin: 0 0 4px;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-handover-title {
  margin: 0 0 8px;
  font-size: var(--text-base);
  font-weight: 650;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.task-handover-description {
  overflow-wrap: anywhere;
  line-height: 1.55;
}
.task-handover-description :deep(> :first-child) { margin-top: 0; }
.task-handover-description :deep(ul),
.task-handover-description :deep(ol) { padding-left: 1.4em; }
.task-handover-description :deep(> :last-child) { margin-bottom: 0; }
.task-handover-empty {
  margin: 0;
  color: var(--fg3);
  font-size: var(--text-sm);
}
.task-handover-raw {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--border);
}
.task-handover-raw summary {
  /* list-item keeps the native disclosure triangle, the only cue it opens. */
  display: list-item;
  cursor: pointer;
  color: var(--fg2);
  font-size: var(--text-sm);
  border-radius: var(--radius-xs);
}
.task-handover-raw summary:hover { color: var(--fg); }
.task-handover-raw summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.task-handover-raw pre {
  margin: 8px 0 0;
  max-height: 360px;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
@media (pointer: coarse) {
  .task-handover-raw summary { padding: 12px 0; }
}
</style>
