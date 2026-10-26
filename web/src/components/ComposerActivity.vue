<template>
  <PopoverRoot :open="openedBy !== null" :modal="false" @update:open="onOpenChange">
  <PopoverAnchor as-child>
  <div
    ref="rootEl"
    class="composer-activity"
    :class="{ 'composer-activity--compact': compact }"
    @keydown.escape="onEscape"
  >
    <div v-if="hasActivity" class="composer-activity-segs">
      <button
        v-if="agentCount > 0"
        type="button"
        class="composer-activity-seg composer-activity-seg--agents"
        :title="agentsTitle"
        :aria-label="agentsTitle"
        aria-haspopup="dialog"
        :aria-expanded="openedBy === 'agents'"
        :aria-controls="popoverId"
        @click="toggle('agents', $event)"
      >
        <ActivityGlyph kind="agent" />
        <span v-if="compact" class="composer-activity-count">{{ agentCount }}</span>
        <span v-else class="composer-activity-label">{{ agentsLabel }}</span>
      </button>
      <button
        v-if="processCount > 0"
        type="button"
        class="composer-activity-seg composer-activity-seg--runs"
        :title="runsTitle"
        :aria-label="runsTitle"
        aria-haspopup="dialog"
        :aria-expanded="openedBy === 'runs'"
        :aria-controls="popoverId"
        @click="toggle('runs', $event)"
      >
        <ActivityGlyph kind="run" />
        <span v-if="compact" class="composer-activity-count">{{ processCount }}</span>
        <span v-else class="composer-activity-label">{{ runsLabel }}</span>
      </button>
      <button
        v-if="outcome"
        type="button"
        class="composer-activity-seg composer-activity-seg--outcome"
        :class="`composer-activity-seg--${outcome.tone}`"
        :title="`${outcome.text}. Show in Chat details`"
        :aria-label="`Background run: ${outcome.text}. Show in Chat details`"
        @click="openDetails"
      >
        <ActivityGlyph :kind="outcome.tone === 'error' ? 'run-failed' : 'run'" />
        <span v-if="!compact" class="composer-activity-label">{{ outcome.text }}</span>
      </button>
    </div>

    <PopoverContent
      v-if="openedBy !== null"
      as-child
      side="top"
      align="end"
      :side-offset="8"
      :collision-padding="8"
      position-strategy="fixed"
      update-position-strategy="always"
      @escape-key-down="onEscape"
      @interact-outside="onInteractOutside"
      @close-auto-focus.prevent
    >
    <div
      :id="popoverId"
      class="composer-activity-popover"
      aria-label="Background activity"
    >
      <template v-if="agentCount > 0">
        <p class="composer-activity-pop-head">Agents</p>
        <router-link
          v-for="sub in agentRows"
          :key="sub.agent_id"
          class="composer-activity-pop-item"
          :to="subagentPath(chatId, sub.agent_id)"
          @click="close(false)"
        >
          <ActivityGlyph kind="agent" />
          <span class="composer-activity-pop-label">{{ subagentLabel(sub) }}</span>
          <small v-if="sub.subagent_type">{{ sub.subagent_type }}</small>
        </router-link>
        <!-- The pushed count can arrive before the running-subagent poll. -->
        <p v-if="!agentRows.length" class="composer-activity-pop-note">Details loading…</p>
      </template>
      <template v-if="processCount > 0">
        <p class="composer-activity-pop-head">Processes</p>
        <!-- A shell row has no transcript to open, so it is never a link. -->
        <button
          v-for="shell in shellRows"
          :key="shell.agent_id"
          type="button"
          class="composer-activity-pop-item"
          :title="subagentLabel(shell)"
          @click="openDetails"
        >
          <ActivityGlyph kind="run" />
          <span class="composer-activity-pop-label">{{ subagentLabel(shell) }}</span>
        </button>
        <button
          v-for="run in runs"
          :key="run.run_id"
          type="button"
          class="composer-activity-pop-item"
          :title="run.cmd.join(' ')"
          @click="openDetails"
        >
          <ActivityGlyph kind="run" />
          <span class="composer-activity-pop-label">{{ backgroundRunLabel(run) }}</span>
          <small>{{ backgroundRunMinutes(run, now) }}</small>
        </button>
      </template>
      <button type="button" class="composer-activity-pop-foot" @click="openDetails">Open Chat details →</button>
    </div>
    </PopoverContent>

    <!-- Announces a run starting or ending, never its ticking age. Always
         mounted so the change is read. -->
    <span class="sr-only" role="status" aria-live="polite">{{ announcement }}</span>
  </div>
  </PopoverAnchor>
  </PopoverRoot>
</template>

<script setup lang="ts">
import { PopoverAnchor, PopoverContent, PopoverRoot } from 'reka-ui'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ActivityGlyph from './ActivityGlyph.vue'
import { backgroundRunLabel, backgroundRunMinutes, backgroundRunOutcome } from '../lib/backgroundRuns'
import { isShellRow, subagentLabel, subagentPath } from '../lib/subagentIds'
import type { BackgroundRunSummary, RunningSubagent } from '../lib/types'

const props = defineProps<{
  chatId: string
  /** Larger of the watcher's pushed count and the poll's non-shell rows. */
  agentCount: number
  /** Poll rows; `shell` rows are processes and are listed under Processes. */
  agents: RunningSubagent[]
  runs: BackgroundRunSummary[]
  /** The run that just ended, for the store's few-second notice; else null. */
  finished: BackgroundRunSummary | null
  now: number
}>()
const emit = defineEmits<{ 'open-details': [] }>()

// Below this composer-bar width the parts fold to glyph + count. The bar is
// measured, not the cluster: the cluster's own width depends on the mode, which
// would lock it in compact.
const COMPACT_BAR_BELOW_PX = 560

const rootEl = ref<HTMLElement | null>(null)
const compact = ref(false)
const openedBy = ref<string | null>(null)
let trigger: HTMLElement | null = null
const popoverId = computed(() => `composer-activity-${props.chatId}`)

const agentsLabel = computed(() => (props.agentCount === 1 ? '1 agent' : `${props.agentCount} agents`))
const agentsTitle = computed(() => (props.agentCount === 1 ? '1 agent running' : `${props.agentCount} agents running`))
const agentRows = computed(() => props.agents.filter(sub => !isShellRow(sub)))
const shellRows = computed(() => props.agents.filter(isShellRow))
const processCount = computed(() => props.runs.length + shellRows.value.length)
const runsLabel = computed(() => (processCount.value === 1 ? '1 shell' : `${processCount.value} shells`))
const runsTitle = computed(() =>
  props.runs.length === 1 && processCount.value === 1
    ? `Background run: ${backgroundRunLabel(props.runs[0])}`
    : `${processCount.value} background runs`,
)
const outcome = computed(() => {
  const done = props.finished
  if (!done || props.runs.some(r => r.run_id === done.run_id)) return null
  return backgroundRunOutcome(done)
})
const hasActivity = computed(() => props.agentCount > 0 || processCount.value > 0 || outcome.value !== null)
const announcement = computed(() => {
  if (outcome.value) return `Background run: ${outcome.value.text}`
  if (!processCount.value) return ''
  const first = props.runs.length ? backgroundRunLabel(props.runs[0]) : subagentLabel(shellRows.value[0])
  const more = processCount.value > 1 ? ` and ${processCount.value - 1} more` : ''
  return `Running in the background: ${first}${more}`
})

function toggle(key: string, event: MouseEvent) {
  if (openedBy.value === key) {
    close(true)
    return
  }
  openedBy.value = key
  trigger = event.currentTarget as HTMLElement
}

function close(returnFocus: boolean) {
  if (openedBy.value === null) return
  openedBy.value = null
  if (returnFocus) trigger?.focus()
  trigger = null
}

function openDetails() {
  close(false)
  emit('open-details')
}

function onOpenChange(next: boolean) {
  if (!next) close(false)
}

// Claim Esc: ChatLayout closes the chat on an unclaimed Esc. Reka listens for
// Escape on window, after ChatLayout's own window listener has already run, so
// the claim has to happen here, on the element the key bubbles through first
// (the popover renders inline, inside the cluster). ModelSelector does the same.
function onEscape(e: KeyboardEvent) {
  if (openedBy.value === null) return
  e.preventDefault()
  e.stopPropagation()
  close(true)
}

// A press on one of the cluster's own parts is the toggle's business (it
// closes, or switches to the other part); letting Reka dismiss first would
// make the click reopen what the press just closed.
function onInteractOutside(e: CustomEvent<{ originalEvent: Event }>) {
  const target = e.detail.originalEvent.target
  if (target instanceof Element && target.closest('.composer-activity-seg')) e.preventDefault()
}

watch(hasActivity, (active) => { if (!active) close(false) })

// The part that opened the popover can vanish (runs ended, agents gone). Close without moving focus: there is nothing to return it to.
const openerRendered = computed(() => {
  const key = openedBy.value
  if (key === null) return true
  if (key === 'agents') return props.agentCount > 0
  return processCount.value > 0
})
watch(openerRendered, (ok) => { if (!ok) close(false) })
watch(() => props.chatId, () => close(false))

let observer: ResizeObserver | null = null
onMounted(() => {
  const bar = rootEl.value?.parentElement
  if (typeof ResizeObserver === 'undefined' || !bar) return
  observer = new ResizeObserver(entries => {
    const width = entries[0]?.contentRect.width
    if (typeof width === 'number') compact.value = width < COMPACT_BAR_BELOW_PX
  })
  observer.observe(bar)
})
onBeforeUnmount(() => { observer?.disconnect() })
</script>

<style scoped>
.composer-activity {
  position: relative;
  display: flex;
  flex: 1 1 0;
  align-items: center;
  justify-content: flex-end;
  min-width: 0;
}

/* Compact parts claim their width; the model chip shrinks instead. The cluster
   still grows: it is the bar's spacer, which keeps Send against the right edge. */
.composer-activity--compact { flex: 1 0 auto; }
.composer-activity--compact .composer-activity-segs { overflow: visible; }

.composer-activity-segs {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 2px;
  min-width: 0;
  overflow: hidden;
}

.composer-activity-seg {
  display: inline-flex;
  flex: 0 1 auto;
  align-items: center;
  gap: 6px;
  min-width: 0;
  height: 28px;
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg2);
  font: inherit;
  font-size: var(--text-sm);
  cursor: pointer;
}

.composer-activity-seg:hover,
.composer-activity-seg[aria-expanded="true"] { background: var(--bg3); color: var(--fg); }
.composer-activity-seg:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.composer-activity-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-activity-count {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 700;
}
.composer-activity-seg--agents .composer-activity-count { color: var(--accent); }

/* Outcome tones. The glyph's own colour is overridden with a 3-class selector
   so it beats ActivityGlyph's scoped rule. */
.composer-activity-seg--ok,
.composer-activity-seg--ok .activity-glyph { color: var(--success); }
.composer-activity-seg--error { color: var(--error); }
.composer-activity-seg--muted,
.composer-activity-seg--muted .activity-glyph { color: var(--fg3); }
/* A settled run's mark is still. */
.composer-activity-seg--outcome .activity-glyph::after { animation: none; }

.composer-activity-popover {
  /* Popper supplies the fixed placement; this is only the surface. */
  z-index: 300;
  display: grid;
  /* minmax(0, …): a long agent name truncates instead of widening its row past
     the popover and pushing the type tag out of it. */
  grid-template-columns: minmax(0, 1fr);
  gap: 2px;
  width: min(280px, calc(100vw - 32px));
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 8px 24px rgb(0 0 0 / 30%);
}

.composer-activity-pop-head {
  margin: 0;
  padding: var(--space-2) var(--space-2) var(--space-1);
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}

.composer-activity-pop-item,
.composer-activity-pop-foot {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
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
  text-decoration: none;
  cursor: pointer;
}

.composer-activity-pop-item:hover,
.composer-activity-pop-foot:hover { background: var(--bg3); }
.composer-activity-pop-item:focus-visible,
.composer-activity-pop-foot:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

.composer-activity-pop-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-activity-pop-item small {
  flex-shrink: 0;
  margin-left: auto;
  color: var(--fg3);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
}

.composer-activity-pop-note {
  margin: 0;
  padding: 0 var(--space-2) var(--space-1);
  color: var(--fg3);
  font-size: var(--text-xs);
}

.composer-activity-pop-foot {
  margin-top: var(--space-1);
  border-top: 1px solid var(--border);
  border-radius: 0 0 var(--radius-sm) var(--radius-sm);
  color: var(--fg2);
}

@media (pointer: coarse) {
  .composer-activity-seg { height: auto; min-height: var(--touch); min-width: var(--touch); justify-content: center; }
  .composer-activity-pop-item,
  .composer-activity-pop-foot { min-height: var(--touch); }
}
</style>