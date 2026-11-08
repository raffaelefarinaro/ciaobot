<script setup lang="ts">
/**
 * A needs-you card: something this chat is blocked on until the user acts
 * (for now, signing in to an MCP server that needs OAuth).
 *
 * The engine owns the action and sends it; the card holds a copy and is keyed
 * by the action id in ChatPanel, so a replaced action is a new card. The card
 * owns the generic controls, Not now and Continue the chat / Done, and the
 * copy. The kind's body owns the primary control and its steps.
 */
import { computed, nextTick, onBeforeUnmount, ref, useId, watch } from 'vue'
import ActivityGlyph from './ActivityGlyph.vue'
import AppIcon from './AppIcon.vue'
import { NEEDS_YOU_BODIES, NEEDS_YOU_ICONS } from './needs-you/registry'
import { actionCopy } from '../lib/chatActions'
import { shouldRestoreFocus } from '../lib/moveToBackground'
import { useProjectStore } from '../stores/projects'
import type { ChatAction, ChatActionKind } from '../lib/types'

/** Kinds whose card has no Not now: the body owns the way out. */
const HIDE_DISMISS: Partial<Record<ChatActionKind, true>> = {}

const props = defineProps<{ chatId: string; action: ChatAction }>()

const store = useProjectStore()
const titleId = useId()
/** The card itself, so the focus can rest on it while a request is out. */
const rootEl = ref<HTMLElement | null>(null)
/** The dismissal control, so the focus can go back to it. */
const dismissEl = ref<HTMLButtonElement | null>(null)

/** A request is out: Not now or Continue is disabled until it answers. */
const pending = ref(false)
/** A retryable failure, in the user's words. */
const errorMessage = ref('')
/** The action changed under the request, so a newer state is on screen. */
const notice = ref('')
/** True once the card is gone. A late answer must not touch it. */
let disposed = false

const copy = computed(() => actionCopy(props.action))
const body = computed(() => NEEDS_YOU_BODIES[props.action.kind] ?? null)
const busy = computed(() => props.action.state === 'working' || props.action.state === 'waiting')
/** Empty on mount, so the first render is not announced; then the new title and reason. */
const liveText = ref('')

watch(() => props.action.state, () => {
  const c = copy.value
  liveText.value = c ? `${c.title}. ${c.why}` : ''
})

// The focus belongs to the card while a request is out, because a disabled
// button drops it onto the page. Moving it to the card keeps it in place.
function holdFocus(): void {
  if (rootEl.value?.contains(document.activeElement)) rootEl.value.focus()
}

async function restoreFocusTo(control: HTMLElement | null): Promise<void> {
  // Only while the focus is still the card's: a user who moved it into the
  // draft must not be pulled back out of it.
  await nextTick()
  if (shouldRestoreFocus(rootEl.value, document.activeElement)) control?.focus()
}

async function dismiss(): Promise<void> {
  if (pending.value) return
  holdFocus()
  pending.value = true
  errorMessage.value = ''
  try {
    const result = await store.dismissChatAction(props.chatId, props.action.id)
    if (disposed) return
    if (result === 'conflict') notice.value = 'Not dismissed: this card changed. The newer state is shown.'
  } catch {
    if (disposed) return
    errorMessage.value = 'This card could not be dismissed. Try again.'
  }
  if (disposed) return
  pending.value = false
  await restoreFocusTo(dismissEl.value)
}

async function continueChat(): Promise<void> {
  if (pending.value) return
  holdFocus()
  pending.value = true
  errorMessage.value = ''
  try {
    const result = await store.continueChatAction(props.chatId, props.action.id)
    if (disposed) return
    if (result === 'conflict') notice.value = 'This card changed. The newer state is shown.'
  } catch {
    if (disposed) return
    errorMessage.value = 'The chat could not be continued. Try again.'
  }
  if (disposed) return
  pending.value = false
  await restoreFocusTo(dismissEl.value)
}

onBeforeUnmount(() => {
  disposed = true
})
</script>

<template>
  <section
    v-if="copy"
    ref="rootEl"
    class="needs-you-card"
    :data-state="action.state"
    :data-kind="action.kind"
    :data-action-id="action.id"
    role="group"
    :aria-labelledby="titleId"
    :aria-busy="busy || undefined"
    tabindex="-1"
  >
    <div class="needs-you-card-head">
      <span class="needs-you-card-glyph" aria-hidden="true">
        <span v-if="busy" class="needs-you-card-spinner" />
        <ActivityGlyph v-else-if="action.state === 'failed'" kind="alert" />
        <AppIcon
          v-else
          :name="action.state === 'done' ? 'check' : (NEEDS_YOU_ICONS[action.kind] ?? 'activity')"
          :size="16"
        />
      </span>
      <div class="needs-you-card-text">
        <p :id="titleId" class="needs-you-card-title">{{ copy.title }}</p>
        <p class="needs-you-card-why">{{ copy.why }}</p>
      </div>
    </div>

    <div class="needs-you-card-controls">
      <component
        :is="body"
        v-if="body"
        :chat-id="chatId"
        :action="action"
        @error="(m: string) => (errorMessage = m)"
        @notice="(m: string) => (notice = m)"
      />

      <div class="needs-you-card-actions">
        <button
          v-if="action.state === 'done'"
          type="button"
          class="needs-you-btn needs-you-btn--primary"
          :disabled="pending"
          @click="continueChat"
        >{{ copy.primaryLabel }}</button>
        <button
          v-if="!HIDE_DISMISS[action.kind]"
          ref="dismissEl"
          type="button"
          class="needs-you-btn needs-you-btn--quiet"
          :disabled="pending"
          @click="dismiss"
        >{{ action.state === 'done' ? 'Done' : 'Not now' }}</button>
        <span class="needs-you-card-meta">{{ copy.meta }}</span>
      </div>
    </div>

    <p v-if="notice" class="needs-you-card-notice" role="status">{{ notice }}</p>
    <p v-if="errorMessage" class="needs-you-card-error" role="alert">{{ errorMessage }}</p>

    <span class="needs-you-card-live sr-only" aria-live="polite">{{ liveText }}</span>
  </section>
</template>

<style scoped>
.needs-you-card {
  /* A flex child with unbreakable content needs min-width: 0, or a long
     server name widens the chat column instead of wrapping inside the card. */
  min-width: 0;
  max-width: 100%;
  flex-shrink: 0;
  display: grid;
  gap: 10px;
  margin: 8px 0;
  padding: 12px 16px;
  border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--border));
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
  text-align: left;
}
.needs-you-card:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.needs-you-card-head {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.needs-you-card-glyph {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
}
.needs-you-card-text { min-width: 0; }
.needs-you-card-title {
  margin: 0;
  color: var(--fg);
  font-weight: 600;
}
.needs-you-card-why {
  margin: 2px 0 0;
  line-height: 1.5;
}
.needs-you-card-spinner {
  display: block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--border-strong);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: needs-you-spin 0.9s linear infinite;
}
@keyframes needs-you-spin {
  to { transform: rotate(360deg); }
}
@media (prefers-reduced-motion: reduce) {
  .needs-you-card-spinner { animation: none; border-color: var(--accent); }
}
.needs-you-card-notice,
.needs-you-card-error {
  margin: 0;
  line-height: 1.5;
}
.needs-you-card-notice { color: var(--fg2); }
.needs-you-card-error { color: var(--fg); }
.needs-you-card-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  min-width: 0;
}
.needs-you-card-actions { display: contents; }
.needs-you-card-meta {
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  color: var(--fg3);
}
.needs-you-card :deep(.needs-you-btn) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 36px;
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-weight: 500;
  text-decoration: none;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.needs-you-card :deep(.needs-you-btn--primary) {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--on-accent);
  font-weight: 600;
}
.needs-you-card :deep(.needs-you-btn--primary:hover:not(:disabled)) {
  background: var(--accent-strong);
  border-color: var(--accent-strong);
}
.needs-you-card :deep(.needs-you-btn--quiet) {
  border-color: transparent;
  color: var(--fg2);
}
.needs-you-card :deep(.needs-you-btn--quiet:hover:not(:disabled)) {
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}
.needs-you-card :deep(.needs-you-btn:disabled) {
  opacity: 0.5;
  cursor: not-allowed;
}
.needs-you-card :deep(.needs-you-btn:focus-visible) {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
@media (pointer: coarse) {
  .needs-you-card :deep(.needs-you-btn) { min-height: var(--touch); }
}
</style>
