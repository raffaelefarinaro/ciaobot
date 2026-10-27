<template>
  <div class="shell-rows" :class="`shell-rows--${variant}`">
    <template v-for="shell in rows" :key="shell.agent_id">
      <div class="shell-row">
        <!-- A shell has no transcript to open, so it is never a link. In the
             popover the label is a button that opens Chat details; Stop is its
             sibling, because a button cannot hold a button. -->
        <component
          :is="variant === 'popover' ? 'button' : 'div'"
          class="shell-row-main"
          :type="variant === 'popover' ? 'button' : undefined"
          :title="subagentLabel(shell)"
          @click="variant === 'popover' && emit('open')"
        >
          <ActivityGlyph kind="run" />
          <span class="shell-row-label">{{ subagentLabel(shell) }}</span>
          <small v-if="minutesSince(shell.started_at || '', now)" class="shell-row-age">{{ minutesSince(shell.started_at || '', now) }}</small>
        </component>
        <button
          v-if="stopTask && shell.stoppable"
          type="button"
          class="shell-row-stop"
          :aria-label="`Stop ${subagentLabel(shell)}`"
          :data-stop-id="shell.agent_id"
          :aria-disabled="isStopping(shell) ? 'true' : undefined"
          @click="stop(shell)"
        >{{ isStopping(shell) ? 'Stopping…' : 'Stop' }}</button>
      </div>
      <p v-if="errorFor(shell)" class="shell-row-note" role="alert">{{ errorFor(shell) }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import ActivityGlyph from './ActivityGlyph.vue'
import { shellTaskId, subagentLabel } from '../lib/subagentIds'
import { minutesSince } from '../lib/time'
import type { RunningSubagent } from '../lib/types'

const props = defineProps<{
  /** Process rows from the running-subagent poll (`shell` / `monitor`). */
  rows: RunningSubagent[]
  now: number
  /** Where the rows are drawn: the composer popover or the Chat details rail. */
  variant: 'popover' | 'rail'
  /** Asks the engine to stop a process row by its bare id; omitted, no row offers Stop. */
  stopTask?: (taskId: string) => unknown
  /** Bare task ids with a stop pending. The state lives in the store: this view unmounts and has a twin. */
  stopping?: Record<string, true>
  /** Bare task id to the message for a stop that failed. */
  errors?: Record<string, string>
}>()
const emit = defineEmits<{ open: [] }>()

const isStopping = (shell: RunningSubagent) => Boolean(props.stopping?.[shellTaskId(shell.agent_id)])
const errorFor = (shell: RunningSubagent) => props.errors?.[shellTaskId(shell.agent_id)]

// `aria-disabled`, not `disabled`: a disabled button drops focus to the body.
function stop(shell: RunningSubagent) {
  if (!props.stopTask || isStopping(shell)) return
  void props.stopTask(shellTaskId(shell.agent_id))
}
</script>

<style scoped>
.shell-rows { display: grid; grid-template-columns: minmax(0, 1fr); gap: 2px; min-width: 0; }

.shell-row { display: flex; align-items: center; min-width: 0; }

.shell-row-main {
  display: flex;
  flex: 1 1 0;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  box-sizing: border-box;
  min-height: 36px;
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
}
button.shell-row-main { cursor: pointer; }
button.shell-row-main:hover { background: var(--bg3); }
button.shell-row-main:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.shell-row-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.shell-row-age {
  flex-shrink: 0;
  margin-left: auto;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

/* Text action on the row's own line, in neutral tones: a process outcome stays
   off the semantic palette. */
.shell-row-stop {
  flex: 0 0 auto;
  min-height: 36px;
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: none;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-xs);
  font-weight: 500;
  cursor: pointer;
}
.shell-row-stop:hover,
.shell-row-stop:focus-visible { background: var(--bg3); color: var(--fg); }
.shell-row-stop:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.shell-row-stop[aria-disabled="true"] { color: var(--fg3); cursor: default; background: none; }

.shell-row-note {
  margin: 0;
  padding: 0 var(--space-2) var(--space-1);
  color: var(--fg3);
  font-size: var(--text-xs);
}

@media (pointer: coarse) {
  .shell-row-main,
  .shell-row-stop { min-height: var(--touch); }
  .shell-row-stop { min-width: var(--touch); }
}
</style>
