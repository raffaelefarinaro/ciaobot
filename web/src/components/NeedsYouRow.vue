<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, useId } from 'vue'

/** One row of Home's "Needs you" list: a title, one line, one primary control
 *  (the `primary` slot) and, only when there is something else to offer, a ⋯
 *  menu. Everything secondary lives in the menu so a row never stacks buttons.
 *
 *  The row root is a focus target (`tabindex="-1"`, `data-home-notice-window`)
 *  because the notification inbox reveals and focuses a notice by its key. */
export interface NeedsYouMenuItem {
  id: string
  label: string
  /** An external page: rendered as a link that opens in a new tab. */
  href?: string
  disabled?: boolean
}

defineProps<{
  title: string
  line: string
  /** The line is an error the operator has to read: announced, and in the error colour. */
  alert?: boolean
  noticeKey?: string
  /** `error` and `blocking` rows carry a marker and read before the others. */
  tone?: '' | 'error' | 'blocking'
  items?: NeedsYouMenuItem[]
}>()

const emit = defineEmits<{ select: [id: string] }>()

const menuId = useId()
const open = ref(false)
const trigger = ref<HTMLButtonElement | null>(null)
const menu = ref<HTMLElement | null>(null)

function menuItems(): HTMLElement[] {
  return menu.value
    ? [...menu.value.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])')]
    : []
}

function onOutside(event: Event): void {
  const target = event.target as Node | null
  if (target && (menu.value?.contains(target) || trigger.value?.contains(target))) return
  close(false)
}

function close(returnFocus: boolean): void {
  if (!open.value) return
  open.value = false
  document.removeEventListener('pointerdown', onOutside, true)
  if (returnFocus) void nextTick(() => trigger.value?.focus())
}

async function toggle(): Promise<void> {
  if (open.value) {
    close(true)
    return
  }
  open.value = true
  document.addEventListener('pointerdown', onOutside, true)
  await nextTick()
  menuItems()[0]?.focus()
}

function onMenuKeydown(event: KeyboardEvent): void {
  const entries = menuItems()
  const at = entries.indexOf(document.activeElement as HTMLElement)
  let next = -1
  if (event.key === 'ArrowDown') next = (at + 1) % entries.length
  else if (event.key === 'ArrowUp') next = (at - 1 + entries.length) % entries.length
  else if (event.key === 'Home') next = 0
  else if (event.key === 'End') next = entries.length - 1
  else if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    close(true)
    return
  } else if (event.key === 'Tab') {
    // Let the browser move focus first; removing the focused node first would
    // drop focus to the page start.
    requestAnimationFrame(() => close(false))
    return
  }
  if (next >= 0) {
    event.preventDefault()
    entries[next]?.focus()
  }
}

function pick(item: NeedsYouMenuItem): void {
  if (item.disabled) return
  // Focus goes back to ⋯; if the row goes away with the action, the list takes it.
  close(true)
  emit('select', item.id)
}

onBeforeUnmount(() => document.removeEventListener('pointerdown', onOutside, true))
</script>

<template>
  <li
    class="needs-you-row"
    :class="{ 'needs-you-row--open': open, [`needs-you-row--${tone}`]: !!tone }"
    data-home-notice-window
    :data-notice-key="noticeKey"
    tabindex="-1"
  >
    <div class="needs-you-text">
      <p class="needs-you-title">
        <span v-if="tone" class="needs-you-marker" aria-hidden="true"></span>
        <span v-if="tone === 'error'" class="sr-only">Error: </span>
        <span v-else-if="tone === 'blocking'" class="sr-only">Blocking: </span>
        <span class="needs-you-title-text" :title="title">{{ title }}</span>
      </p>
      <p class="needs-you-line" :class="{ 'needs-you-line--alert': alert }" :role="alert ? 'alert' : undefined" :title="line">{{ line }}</p>
    </div>
    <div class="needs-you-controls">
      <div class="needs-you-primary"><slot name="primary" /></div>
      <div v-if="items && items.length" class="needs-you-menu">
        <button
          ref="trigger"
          type="button"
          class="btn-icon needs-you-more"
          :aria-label="`More actions for ${title}`"
          aria-haspopup="menu"
          :aria-expanded="open ? 'true' : 'false'"
          :aria-controls="open ? menuId : undefined"
          @click="toggle"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" />
          </svg>
        </button>
        <div
          v-if="open"
          :id="menuId"
          ref="menu"
          class="needs-you-popover"
          role="menu"
          :aria-label="`Actions for ${title}`"
          @keydown="onMenuKeydown"
        >
          <template v-for="item in items" :key="item.id">
            <a
              v-if="item.href"
              class="needs-you-item"
              role="menuitem"
              :href="item.href"
              target="_blank"
              rel="noopener noreferrer"
              @click="pick(item)"
            >{{ item.label }}</a>
            <button
              v-else
              type="button"
              class="needs-you-item"
              role="menuitem"
              :aria-disabled="item.disabled ? 'true' : undefined"
              @click="pick(item)"
            >{{ item.label }}</button>
          </template>
        </div>
      </div>
    </div>
  </li>
</template>

<style scoped>
.needs-you-row {
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  column-gap: var(--space-4);
  row-gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--border);
  list-style: none;
}
.needs-you-row:first-child { border-top: 0; }
.needs-you-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
  border-radius: var(--radius);
}
/* The open menu has to paint over the rows after it. */
.needs-you-row--open { z-index: 2; }

.needs-you-text { min-width: 0; }

.needs-you-title {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0;
  font-size: var(--text-base);
  font-weight: 650;
  line-height: 1.4;
  color: var(--fg);
}
.needs-you-title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Tone is a shape and a word for assistive tech, never colour alone. */
.needs-you-marker {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: var(--radius-pill);
  background: var(--warning);
}
.needs-you-row--error .needs-you-marker { background: var(--error); }

.needs-you-line {
  margin: var(--space-1) 0 0;
  font-size: var(--text-sm);
  line-height: 1.4;
  color: var(--fg2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* A refusal has to be readable, not clipped: allow two lines. */
.needs-you-line--alert {
  color: var(--error);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  white-space: normal;
}

.needs-you-controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

/* Equal-width primaries: whatever the label, every row's lead control is the
   same size, so the column of buttons reads as one edge. */
.needs-you-primary { display: flex; align-items: center; gap: var(--space-2); }
.needs-you-primary :deep(.btn-small),
.needs-you-primary :deep(.btn-primary),
.needs-you-primary :deep(.host-terminal button) {
  box-sizing: border-box;
  min-width: 9rem;
  min-height: var(--touch);
  justify-content: center;
  text-align: center;
  text-decoration: none;
  white-space: nowrap;
}

.needs-you-menu { position: relative; }
.needs-you-more { flex: 0 0 auto; color: var(--fg2); }
.needs-you-more:hover { color: var(--fg); }

.needs-you-popover {
  position: absolute;
  top: calc(100% + var(--space-1));
  right: 0;
  z-index: 3;
  min-width: 12rem;
  max-width: min(20rem, 80vw);
  padding: var(--space-1);
  display: flex;
  flex-direction: column;
  background: var(--bg-elev, var(--bg2));
  border: 1px solid var(--border-strong, var(--border));
  border-radius: var(--radius);
  box-shadow: 0 12px 32px -14px rgb(0 0 0 / 45%);
}
.needs-you-item {
  display: flex;
  align-items: center;
  min-height: var(--touch);
  padding: 0 var(--space-3);
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
.needs-you-item:hover,
.needs-you-item:focus-visible { background: var(--bg3); }
.needs-you-item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.needs-you-item[aria-disabled='true'] { color: var(--fg3); cursor: default; }

/* Narrow list (a phone, or the pane squeezed by a docked panel): the text takes
   the full row and the controls sit under it, primary stretching to fill. */
@container needs-you (max-width: 520px) {
  .needs-you-row { grid-template-columns: minmax(0, 1fr); }
  .needs-you-controls { width: 100%; }
  .needs-you-primary { flex: 1 1 auto; }
  .needs-you-primary :deep(.btn-small),
  .needs-you-primary :deep(.btn-primary),
  .needs-you-primary :deep(.host-terminal),
  .needs-you-primary :deep(.host-terminal button) { width: 100%; }
  .needs-you-menu { margin-left: auto; }
}

@media (prefers-reduced-motion: no-preference) {
  .needs-you-popover { animation: needs-you-pop 120ms var(--ease); }
}
@keyframes needs-you-pop {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>
