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
          :aria-disabled="stopping.has(shell.agent_id) ? 'true' : undefined"
          @click="stop(shell)"
        >{{ stopping.has(shell.agent_id) ? 'Stopping…' : 'Stop' }}</button>
      </div>
      <p v-if="stopErrors[shell.agent_id]" class="shell-row-note" role="alert">{{ stopErrors[shell.agent_id] }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, reactive, watch } from 'vue'
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
  /** Stops a process row by its bare id; omitted, no row offers Stop. */
  stopTask?: (taskId: string) => Promise<void>
}>()
const emit = defineEmits<{
  open: []
  /** Text for the owner's live region: "Stopping x", or '' once it is settled. */
  notice: [text: string]
}>()

// Stop keeps its "Stopping…" state until the row leaves the poll: the engine
// answers before the provider has reported the task ended.
const stopping = reactive(new Set<string>())
const stopErrors = reactive<Record<string, string>>({})
const stopTimers = new Map<string, ReturnType<typeof setTimeout>>()
// A task the provider never reports ended must not leave Stop locked for good.
const STOP_LOCK_MS = 15_000

function releaseStop(id: string) {
  const timer = stopTimers.get(id)
  if (timer) clearTimeout(timer)
  stopTimers.delete(id)
  stopping.delete(id)
}

// `aria-disabled`, not `disabled`: a disabled button drops focus to the body.
async function stop(shell: RunningSubagent) {
  const id = shell.agent_id
  if (!props.stopTask || stopping.has(id)) return
  stopping.add(id)
  emit('notice', `Stopping ${subagentLabel(shell)}`)
  delete stopErrors[id]
  stopTimers.set(id, setTimeout(() => releaseStop(id), STOP_LOCK_MS))
  try {
    await props.stopTask(shellTaskId(id))
  } catch {
    releaseStop(id)
    emit('notice', '')
    stopErrors[id] = 'Could not stop this process. Try again.'
  }
}

// Pre-flush: runs before the DOM drops a row, so the owner's focus handoff
// (which reads the focused Stop) still sees it.
watch(() => props.rows.map(r => r.agent_id), (ids) => {
  const live = new Set(ids)
  for (const id of [...stopping]) if (!live.has(id)) releaseStop(id)
  for (const id of Object.keys(stopErrors)) if (!live.has(id)) delete stopErrors[id]
  if (!stopping.size) emit('notice', '')
}, { flush: 'pre' })

onBeforeUnmount(() => {
  for (const timer of stopTimers.values()) clearTimeout(timer)
  stopTimers.clear()
})
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
