<template>
  <div class="subagent-card" :class="`subagent-card--${card.state}`">
    <button
      type="button"
      class="subagent-card-toggle"
      :aria-expanded="expanded"
      :aria-controls="panelId"
      @click="expanded = !expanded"
    >
      <svg class="subagent-card-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      <span class="subagent-card-title" :title="title">{{ title }}</span>
      <span v-if="card.agent_type" class="subagent-card-type">{{ card.agent_type }}</span>
      <span class="subagent-card-state">
        <ActivityGlyph v-if="card.state === 'running'" kind="working" />
        <svg
          v-else-if="card.state === 'done'"
          class="subagent-card-mark"
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
        <span class="subagent-card-state-label">{{ stateLabel }}</span>
      </span>
      <span class="subagent-card-count">{{ stepsLabel }}</span>
    </button>
    <div v-if="expanded" :id="panelId" class="subagent-card-body">
      <template v-for="(step, j) in shownSteps" :key="j">
        <div v-if="step.kind === 'activity'" class="activity-line">{{ step.line }}</div>
        <button
          v-else
          type="button"
          class="subagent-card-file"
          :title="step.file_path"
          @click="emit('open-file', step.file_path)"
        >
          <span class="subagent-card-file-action">{{ step.action }}</span>
          <span class="subagent-card-file-name">{{ step.file_path }}</span>
        </button>
      </template>
      <div v-if="moreCount > 0" class="subagent-card-more">+{{ moreCount }} more</div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * One subagent call as a collapsible card in the activity trace.
 *
 * The card's data comes from `lib/subagentCard.ts`, the same shape from a live
 * stream and from reloaded history. It is open while the subagent runs and
 * folds once it finishes; the reader can open it again. Everything from the
 * subagent is rendered as text, never as HTML.
 */
import { computed, ref, watch } from 'vue'
import ActivityGlyph from './ActivityGlyph.vue'
import { SUBAGENT_STEP_LIMIT, subagentTitle, type SubagentCard } from '../lib/subagentCard'

const props = defineProps<{ card: SubagentCard }>()
const emit = defineEmits<{ 'open-file': [path: string] }>()

let panelSeq = 0
const panelId = `subagent-card-body-${++panelSeq}`

const expanded = ref(props.card.state === 'running')
watch(() => props.card.state, (state) => {
  if (state !== 'running') expanded.value = false
})

const title = computed(() => subagentTitle(props.card))
const stateLabel = computed(() => {
  if (props.card.state === 'running') return 'Running'
  return props.card.state === 'done' ? 'Done' : 'Failed'
})
const stepsLabel = computed(() => {
  const count = props.card.step_count
  return `${count} step${count === 1 ? '' : 's'}`
})
const shownSteps = computed(() => props.card.steps.slice(0, SUBAGENT_STEP_LIMIT))
const moreCount = computed(() => Math.max(0, props.card.step_count - shownSteps.value.length))
</script>

<style scoped>
.subagent-card {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  margin: var(--space-1) 0;
  min-width: 0;
}

.subagent-card--failed {
  border-color: var(--error);
}

.subagent-card-toggle {
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

.subagent-card-toggle:hover {
  background: var(--bg3);
}

.subagent-card-toggle:focus-visible,
.subagent-card-file:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.subagent-card-chevron {
  flex: 0 0 auto;
  color: var(--fg3);
  transition: transform 150ms var(--ease);
}

[aria-expanded='true'] > .subagent-card-chevron {
  transform: rotate(90deg);
}

.subagent-card-title {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  font-weight: 600;
}

.subagent-card-type {
  flex: 0 0 auto;
  padding: 0 var(--space-1);
  border: 1px solid var(--border);
  border-radius: var(--radius-xs);
  color: var(--fg2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.subagent-card-state {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  color: var(--fg2);
  font-size: var(--text-xs);
}

.subagent-card--failed .subagent-card-state {
  color: var(--error-text);
}

.subagent-card-count {
  flex: 0 0 auto;
  color: var(--fg3);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}

.subagent-card-body {
  display: grid;
  gap: 2px;
  padding: 0 var(--space-3) var(--space-2) calc(var(--space-3) + 14px);
}

/* The trace's activity-line rule lives in the panes' scoped chatTrace.css, which
   does not reach this component, so its own copy is here. */
.activity-line {
  padding: 1px 0;
  color: var(--fg2);
  font-family: var(--font);
  font-size: var(--text-sm);
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .subagent-card-chevron {
    transition: none;
  }
}

.subagent-card-file {
  display: flex;
  align-items: baseline;
  gap: var(--space-2);
  min-height: var(--touch);
  padding: 0;
  background: none;
  border: 0;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  min-width: 0;
}

.subagent-card-file-action {
  flex: 0 0 auto;
  color: var(--fg3);
  font-size: var(--text-xs);
}

.subagent-card-file-name {
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.subagent-card-more {
  color: var(--fg3);
  font-size: var(--text-xs);
}
</style>
