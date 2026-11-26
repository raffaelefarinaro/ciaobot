<template>
  <div class="shell-card" :class="`shell-card--${card.state}`">
    <button
      type="button"
      class="shell-card-toggle"
      :aria-expanded="expanded"
      :aria-controls="panelId"
      @click="expanded = !expanded"
    >
      <svg class="shell-card-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      <span class="shell-card-command" :title="title">{{ title }}</span>
      <span
        v-if="card.background && card.state === 'running'"
        class="shell-card-badge"
        :title="BACKGROUND_TIP"
      >running in the background</span>
      <span class="shell-card-state">
        <ActivityGlyph v-if="card.state === 'running'" kind="working" />
        <svg
          v-else-if="card.state === 'done'"
          class="shell-card-mark"
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        ><path d="m5 12 5 5 9-10" /></svg>
        <ActivityGlyph v-else kind="alert" />
        <span class="shell-card-state-label">{{ stateLabel }}</span>
        <span v-if="card.exit_code !== null" class="shell-card-exit">exit {{ card.exit_code }}</span>
      </span>
    </button>
    <div v-if="expanded" :id="panelId" class="shell-card-body">
      <pre v-if="card.output_tail" class="shell-card-output" tabindex="0">{{ card.output_tail }}</pre>
      <div v-else class="shell-card-empty">{{ card.state === 'running' ? 'No output yet.' : 'No output.' }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * One shell call as a collapsible card in the activity trace.
 *
 * The card's data comes from `lib/shellCard.ts`, the same shape from a live
 * stream and from reloaded history. It is open while the shell runs and folds
 * once it finishes; the reader can open it again. The command and the output
 * tail are rendered as text, never as HTML.
 */
import { computed, ref, watch } from 'vue'
import ActivityGlyph from './ActivityGlyph.vue'
import { shellTitle, type ShellCard } from '../lib/shellCard'

const props = defineProps<{ card: ShellCard }>()

const BACKGROUND_TIP = 'Ciaobot learns that this shell ended only when the agent reports back.'

let panelSeq = 0
const panelId = `shell-card-body-${++panelSeq}`

const expanded = ref(props.card.state === 'running')
watch(() => props.card.state, (state) => {
  if (state !== 'running') expanded.value = false
})

const title = computed(() => shellTitle(props.card))
const stateLabel = computed(() => {
  if (props.card.state === 'running') return 'Running'
  return props.card.state === 'done' ? 'Done' : 'Failed'
})
</script>

<style scoped>
.shell-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  margin: var(--space-1) 0;
  min-width: 0;
}

.shell-card--failed {
  border-color: var(--error);
}

.shell-card-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--touch);
  padding: 0 var(--space-3);
  background: none;
  border: 0;
  border-radius: var(--radius-sm);
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  touch-action: manipulation;
}

.shell-card-toggle:hover {
  background: var(--bg3);
}

.shell-card-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.shell-card-chevron {
  flex: 0 0 auto;
  color: var(--fg3);
  transition: transform 150ms var(--ease);
}

[aria-expanded='true'] > .shell-card-chevron {
  transform: rotate(90deg);
}

.shell-card-command {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.shell-card-badge {
  flex: 0 0 auto;
  padding: 0 var(--space-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-size: var(--text-xs);
  white-space: nowrap;
}

.shell-card-state {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--fg2);
  font-size: var(--text-xs);
}

.shell-card--failed .shell-card-state {
  color: var(--error-text);
}

.shell-card-exit {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

.shell-card-body {
  padding: 0 var(--space-3) var(--space-2) calc(var(--space-3) + 14px);
}

/* The output is text in a preformatted block: long lines wrap, a long tail scrolls. */
.shell-card-output {
  margin: 0;
  max-height: 16em;
  overflow: auto;
  padding: var(--space-2);
  background: var(--bg);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.shell-card-empty {
  color: var(--fg3);
  font-size: var(--text-xs);
}

@media (prefers-reduced-motion: reduce) {
  .shell-card-chevron {
    transition: none;
  }
}
</style>
