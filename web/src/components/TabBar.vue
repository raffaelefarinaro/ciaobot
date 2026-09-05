<template>
  <!-- A tablist, not a nav landmark: these switch panels in place rather than
       navigating. `role="tab"` is only meaningful inside `role="tablist"`, and
       each tab owns the panel it names via aria-controls/aria-labelledby. -->
  <div class="tab-bar" role="tablist" :aria-label="label">
    <button
      v-for="tab in tabs"
      :key="tab.key"
      :id="tabId(tab.key)"
      type="button"
      class="tab-bar-tab"
      :class="{ active: modelValue === tab.key }"
      role="tab"
      :aria-selected="modelValue === tab.key"
      :aria-controls="panelId(tab.key)"
      :tabindex="modelValue === tab.key ? 0 : -1"
      :data-tab="tab.key"
      @click="select(tab.key)"
      @keydown="onKeydown"
    >
      {{ tab.label }}
      <span v-if="tab.count !== undefined && tab.count !== null" class="tab-bar-count">{{ tab.count }}</span>
    </button>
  </div>
</template>

<script setup lang="ts" generic="K extends string">
// One tab bar for the whole app. Three copies of this markup, styling and
// roving-tabindex handler had grown up independently (project sections,
// proposal review, memory-map review), and the newest of them shipped without
// the keyboard behaviour at all — while rendering directly above one of the
// others, where any styling drift is visible side by side.

// Generic over the key union so a caller whose state is
// `'proposals' | 'retirement'` keeps that type through v-model, instead of
// silently widening to string on every emit.
export interface TabSpec<K extends string = string> {
  key: K
  label: string
  /** Rendered as a pill after the label. `null`/undefined hides it. */
  count?: number | null
}

const props = defineProps<{
  modelValue: K
  tabs: TabSpec<K>[]
  /** Accessible name for the tablist. */
  label: string
  /** Prefix for the generated tab/panel ids, so two bars on one page differ. */
  idPrefix: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', key: K): void }>()

const TAB_KEYS = ['ArrowLeft', 'ArrowRight', 'Home', 'End']

function tabId(key: K): string {
  return `${props.idPrefix}-tab-${key}`
}

function panelId(key: K): string {
  return `${props.idPrefix}-panel-${key}`
}

function select(key: K): void {
  if (key !== props.modelValue) emit('update:modelValue', key)
}

// Roving tabindex: the bar is a single Tab stop, and Left/Right/Home/End move
// between tabs (and switch to them, which is the expected behaviour for a
// tablist whose panels are cheap to render).
function onKeydown(event: KeyboardEvent): void {
  if (!TAB_KEYS.includes(event.key)) return
  const current = event.currentTarget as HTMLElement | null
  const bar = current?.parentElement
  if (!current || !bar) return
  const items = Array.from(bar.querySelectorAll<HTMLElement>('[role="tab"]'))
  const index = items.indexOf(current)
  if (index < 0) return
  event.preventDefault()
  let next = index
  if (event.key === 'ArrowLeft') next = (index - 1 + items.length) % items.length
  else if (event.key === 'ArrowRight') next = (index + 1) % items.length
  else if (event.key === 'Home') next = 0
  else next = items.length - 1
  const target = items[next]
  // `data-tab` round-trips through the DOM as a string; the bar only ever
  // rendered keys from `tabs`, so narrowing back to K is sound.
  const key = target?.dataset.tab as K | undefined
  if (!key) return
  select(key)
  target.focus()
}

defineExpose({ tabId, panelId })
</script>

<style scoped>
.tab-bar {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--border);
  overflow-x: auto;
}

.tab-bar-tab {
  min-height: var(--touch);
  padding: var(--space-2) var(--space-3);
  border: 0;
  border-bottom: 2px solid transparent;
  background: transparent;
  color: var(--fg2);
  cursor: pointer;
  font: 600 var(--text-sm) var(--font);
  white-space: nowrap;
}
.tab-bar-tab:hover { color: var(--fg); background: var(--bg3); }
.tab-bar-tab.active {
  border-bottom-color: var(--accent);
  color: var(--fg);
}
.tab-bar-tab:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }

.tab-bar-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: calc(var(--text-xs) + var(--space-2));
  min-height: calc(var(--text-xs) + var(--space-1));
  margin-left: var(--space-1);
  padding: 0 var(--space-1);
  border-radius: var(--radius-pill);
  background: var(--bg3);
  color: var(--fg2);
  font-size: var(--text-xs);
  font-variant-numeric: tabular-nums;
}
.tab-bar-tab.active .tab-bar-count { color: var(--fg); }
</style>
