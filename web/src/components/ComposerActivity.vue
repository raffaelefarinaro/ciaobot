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
        v-if="agents.length > 0"
        type="button"
        class="composer-activity-seg composer-activity-seg--agents"
        :title="agentsTitle"
        :aria-label="agentsTitle"
        aria-haspopup="dialog"
        :aria-expanded="openedBy === 'agents'"
        :aria-controls="popoverId"
        @click="toggle('agents', $event)"
      >
        <ActivityGlyph kind="working" />
        <span v-if="compact" class="composer-activity-count">{{ agents.length }}</span>
        <span v-else class="composer-activity-label">{{ agentsLabel }}</span>
      </button>
      <button
        v-if="shells.length > 0"
        type="button"
        class="composer-activity-seg composer-activity-seg--runs"
        :title="shellsTitle"
        :aria-label="shellsTitle"
        aria-haspopup="dialog"
        :aria-expanded="openedBy === 'shells'"
        :aria-controls="popoverId"
        @click="toggle('shells', $event)"
      >
        <ActivityGlyph kind="run" />
        <span v-if="compact" class="composer-activity-count">{{ shells.length }}</span>
        <span v-else class="composer-activity-label">{{ shellsLabel }}</span>
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
      aria-label="Running work"
    >
      <template v-if="agents.length > 0">
        <p class="composer-activity-pop-head">Agents</p>
        <ul class="composer-activity-pop-list">
          <li v-for="row in agents" :key="row.tool_use_id" class="composer-activity-pop-item">
            <ActivityGlyph kind="working" />
            <span class="composer-activity-pop-label">{{ row.label }}</span>
            <small v-if="row.detail" class="composer-activity-pop-detail">{{ row.detail }}</small>
            <small v-if="elapsed(row)" class="composer-activity-pop-age">{{ elapsed(row) }}</small>
          </li>
        </ul>
      </template>
      <template v-if="shells.length > 0">
        <p class="composer-activity-pop-head">Shells</p>
        <ul class="composer-activity-pop-list">
          <li v-for="row in shells" :key="row.tool_use_id" class="composer-activity-pop-item">
            <ActivityGlyph kind="run" />
            <span class="composer-activity-pop-label">{{ row.label }}</span>
            <small v-if="elapsed(row)" class="composer-activity-pop-age">{{ elapsed(row) }}</small>
          </li>
        </ul>
      </template>
    </div>
    </PopoverContent>
  </div>
  </PopoverAnchor>
  </PopoverRoot>
</template>

<script setup lang="ts">
import { PopoverAnchor, PopoverContent, PopoverRoot } from 'reka-ui'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import ActivityGlyph from './ActivityGlyph.vue'
import { elapsedLabel, type RunningWorkRow } from '../lib/runningWork'

const props = defineProps<{
  chatId: string
  /** Running subagents of the chat (its running subagent cards). */
  agents: RunningWorkRow[]
  /** Running background shells of the chat (its running, backgrounded shell cards). */
  shells: RunningWorkRow[]
  /** The shared clock, ticking while work runs, so elapsed times advance. */
  now: number
}>()

// Below this composer-bar width the parts fold to glyph + count. The bar is
// measured, not the cluster: the cluster's own width depends on the mode, which
// would lock it in compact.
const COMPACT_BAR_BELOW_PX = 560

const rootEl = ref<HTMLElement | null>(null)
const compact = ref(false)
const openedBy = ref<'agents' | 'shells' | null>(null)
let trigger: HTMLElement | null = null
const popoverId = computed(() => `composer-activity-${props.chatId}`)

const agentsLabel = computed(() => (props.agents.length === 1 ? '1 agent' : `${props.agents.length} agents`))
const agentsTitle = computed(() => (props.agents.length === 1 ? '1 agent running' : `${props.agents.length} agents running`))
const shellsLabel = computed(() => (props.shells.length === 1 ? '1 shell' : `${props.shells.length} shells`))
const shellsTitle = computed(() => (props.shells.length === 1 ? '1 shell running' : `${props.shells.length} shells running`))
const hasActivity = computed(() => props.agents.length > 0 || props.shells.length > 0)

function elapsed(row: RunningWorkRow): string {
  return elapsedLabel(row.started_at, props.now)
}

function toggle(key: 'agents' | 'shells', event: MouseEvent) {
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

// The part that opened the popover can vanish (the last shell ended). Close without
// moving focus: there is nothing left to return it to.
const openerRendered = computed(() => {
  const key = openedBy.value
  if (key === null) return true
  return key === 'agents' ? props.agents.length > 0 : props.shells.length > 0
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
onBeforeUnmount(() => {
  observer?.disconnect()
})
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

.composer-activity-popover {
  /* Popper supplies the fixed placement; this is only the surface. */
  z-index: 300;
  display: grid;
  gap: 2px;
  width: min(280px, calc(100vw - 32px));
  max-height: min(360px, 60vh);
  overflow-y: auto;
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

.composer-activity-pop-list {
  display: grid;
  gap: 2px;
  margin: 0;
  padding: 0;
  list-style: none;
}

/* A row is text, not a control: nothing in the popover acts, so no hover or focus. */
.composer-activity-pop-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  min-height: 36px;
  padding: 0 var(--space-2);
  color: var(--fg);
  font-size: var(--text-sm);
}

.composer-activity-pop-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-activity-pop-detail,
.composer-activity-pop-age {
  flex-shrink: 0;
  color: var(--fg3);
  font-size: var(--text-xs);
}

.composer-activity-pop-detail { margin-left: auto; font-family: var(--font-mono); }
.composer-activity-pop-age { margin-left: auto; font-family: var(--font-mono); }
.composer-activity-pop-detail + .composer-activity-pop-age { margin-left: var(--space-1); }

@media (pointer: coarse) {
  .composer-activity-seg { height: auto; min-height: var(--touch); min-width: var(--touch); justify-content: center; }
  .composer-activity-pop-item { min-height: var(--touch); }
}
</style>
