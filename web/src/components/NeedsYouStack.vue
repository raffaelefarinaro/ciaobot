<script setup lang="ts">
/**
 * The needs-you cards of one chat, laid out so they never take the chat over.
 *
 * One card is shown as it is. Two or more untouched sign-ins (an `mcp_auth`
 * card still `open` that the user has not reached into) fold under one quiet
 * row that names them, with Show and Not now for all: a provider can report
 * many servers at once, and a card per server above the composer squeezed the
 * transcript to nothing. A card the user acted on (working, waiting, failed,
 * done, or a loose one they focused) and any other kind stays out of the fold,
 * so the sign-in in progress is never hidden. Focusing a card inside the fold
 * does not take it out: moving it on mousedown would pull it from under the
 * pointer and lose the click; it leaves once the engine moves its state on. The region is height-capped and scrolls
 * on its own, so the transcript always keeps its room. When a card appears or changes state, the region scrolls itself
 * (instantly under reduced motion) so that card's key control is in view: a `[data-needs-you-primary]` element, else its
 * primary button. It never moves the focus, and it leaves the scroll alone while the focus is in another card.
 *
 * Every card renders in one keyed list, folded or not: a card that leaves the
 * fold is moved, never rebuilt, so its focus and its local state survive.
 */
import { computed, nextTick, onMounted, ref, useId, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import NeedsYouCard from './NeedsYouCard.vue'
import { mcpServerName } from '../lib/chatActions'
import { useProjectStore } from '../stores/projects'
import type { ChatAction } from '../lib/types'

/** How many untouched sign-ins it takes to fold them under one row. */
const FOLD_AT = 2

const props = defineProps<{ chatId: string; actions: ChatAction[] }>()
/** The fold held the focus and went away: the composer should take it. */
const emit = defineEmits<{ releaseFocus: [] }>()

const store = useProjectStore()
const listId = useId()
const stackEl = ref<HTMLElement | null>(null)
const expanded = ref(false)
const dismissing = ref(false)
const errorMessage = ref('')
/** Cards the user reached into: they stay where they are, out of the fold. */
const touched = ref(new Set<string>())

function untouchedSignIn(a: ChatAction): boolean {
  return a.kind === 'mcp_auth' && a.state === 'open' && !touched.value.has(a.id)
}

const foldable = computed(() => props.actions.filter(untouchedSignIn))
const folded = computed(() => foldable.value.length >= FOLD_AT)
const foldedIds = computed(() => new Set(folded.value ? foldable.value.map(a => a.id) : []))
/** Loose cards first, then the folded ones under the row; one list either way. */
const ordered = computed(() => [
  ...props.actions.filter(a => !foldedIds.value.has(a.id)),
  ...props.actions.filter(a => foldedIds.value.has(a.id)),
])
const firstFolded = computed(() => ordered.value.find(a => foldedIds.value.has(a.id))?.id ?? '')
const names = computed(() => foldable.value.map(a => mcpServerName(a.params.server ?? '')).filter(Boolean))
const summary = computed(() => `${foldable.value.length} connectors need a sign-in`)

// A fold that dissolves forgets its toggle and its error: the next one starts closed and clean.
watch(folded, (now) => {
  if (!now) {
    expanded.value = false
    errorMessage.value = ''
  }
})

/** Room left between the revealed control and the stack's edge. */
const REVEAL_PAD = 8
/** Each card's last seen state, so a reveal follows only a new card or a state change. */
let seen = new Map<string, string>()

/** A card's key control: the body's marked one, else its primary button. */
function revealTarget(card: HTMLElement): HTMLElement | null {
  return card.querySelector<HTMLElement>('[data-needs-you-primary]')
    ?? card.querySelector<HTMLElement>('.needs-you-btn--primary')
}

/**
 * Scroll the stack, never the page, so the newest changed card's key control
 * is in view. Never moves the focus, and leaves the stack alone while the
 * focus is in another card.
 */
function reveal(ids: Set<string>): void {
  const stack = stackEl.value
  if (!stack || !ids.size) return
  const cards = [...stack.querySelectorAll<HTMLElement>('.needs-you-card')]
    .filter(c => ids.has(c.dataset.actionId ?? '') && !(foldedIds.value.has(c.dataset.actionId ?? '') && !expanded.value))
  const card = cards[cards.length - 1]
  if (!card) return
  const active = document.activeElement
  if (active && stack.contains(active) && !card.contains(active)) return
  const target = revealTarget(card)
  if (!target) return
  const s = stack.getBoundingClientRect()
  const t = target.getBoundingClientRect()
  let delta = 0
  if (t.bottom + REVEAL_PAD > s.bottom) delta = t.bottom + REVEAL_PAD - s.bottom
  else if (t.top - REVEAL_PAD < s.top) delta = t.top - REVEAL_PAD - s.top
  const top = Math.max(0, stack.scrollTop + delta)
  if (top === stack.scrollTop) return
  const reduce = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  stack.scrollTo({ top, behavior: reduce ? 'auto' : 'smooth' })
}

/** The ids that are new or changed state since the last call. */
function changedIds(): Set<string> {
  const next = new Map(props.actions.map(a => [a.id, a.state] as const))
  const changed = new Set([...next].filter(([id, st]) => seen.get(id) !== st).map(([id]) => id))
  seen = next
  return changed
}

onMounted(() => reveal(changedIds()))
watch(() => props.actions.map(a => `${a.id}:${a.state}`).join('|'), () => reveal(changedIds()), { flush: 'post' })

function touch(event: FocusEvent): void {
  const id = (event.target as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')?.dataset.actionId
  // Only a card already shown loose: re-ordering a folded one mid-click would lose the click.
  if (id && !touched.value.has(id) && !foldedIds.value.has(id)) {
    touched.value = new Set([...touched.value, id])
  }
}

async function dismissAll(): Promise<void> {
  if (dismissing.value) return
  // The button is disabled below, which would drop the focus onto the page.
  const fold = stackEl.value?.querySelector<HTMLElement>('.needs-you-fold')
  if (fold?.contains(document.activeElement)) fold.focus()
  dismissing.value = true
  errorMessage.value = ''
  // One at a time: each answer replaces the chat's action list, and a
  // conflict only means that card changed, which it then shows itself.
  let failed = 0
  for (const action of [...foldable.value]) {
    try {
      await store.dismissChatAction(props.chatId, action.id)
    } catch {
      failed += 1
    }
  }
  dismissing.value = false
  if (failed) errorMessage.value = `${failed} could not be dismissed. Try again.`
  await nextTick()
  const active = document.activeElement
  if (!folded.value && (active === document.body || active === null)) emit('releaseFocus')
}
</script>

<template>
  <div v-if="actions.length" ref="stackEl" class="needs-you-stack" @focusin="touch">
    <template v-for="action in ordered" :key="action.id">
      <section
        v-if="action.id === firstFolded"
        class="needs-you-fold"
        :class="{ 'is-expanded': expanded }"
        role="group"
        :aria-label="summary"
        tabindex="-1"
      >
        <span class="needs-you-fold-glyph" aria-hidden="true"><AppIcon name="shield" :size="14" /></span>
        <p class="needs-you-fold-text">
          <span class="needs-you-fold-title">{{ summary }}</span>
          <span class="needs-you-fold-names" :title="names.join(', ')">{{ names.join(', ') }}</span>
        </p>
        <button
          type="button"
          class="needs-you-fold-btn"
          :aria-expanded="expanded"
          :aria-controls="expanded ? listId : undefined"
          @click="expanded = !expanded"
        >{{ expanded ? 'Hide' : 'Show' }}</button>
        <button
          type="button"
          class="needs-you-fold-btn needs-you-fold-btn--quiet"
          :disabled="dismissing"
          @click="dismissAll"
        >Not now for all</button>
        <p v-if="errorMessage" class="needs-you-fold-error" role="alert">{{ errorMessage }}</p>
      </section>
      <NeedsYouCard
        v-show="!foldedIds.has(action.id) || expanded"
        :id="action.id === firstFolded ? listId : undefined"
        :class="{ 'needs-you-card--folded': foldedIds.has(action.id) }"
        :chat-id="chatId"
        :action="action"
      />
    </template>
  </div>
</template>

<style scoped>
.needs-you-stack {
  /* The transcript above is the chat; this region may take at most about
     two cards' height and scrolls inside itself beyond that. */
  flex-shrink: 0;
  min-width: 0;
  max-height: min(42vh, 380px);
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
  /* overflow clips the cards' 2px focus ring at offset 2px: give it room. */
  margin-inline: -4px;
  padding-inline: 4px;
}
.needs-you-fold {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  min-width: 0;
  margin: 8px 0 0;
  padding: 8px 8px 8px 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 50%, var(--border));
  border-radius: var(--radius);
  background: var(--bg2);
  color: var(--fg2);
  font-size: var(--text-sm);
}
.needs-you-fold:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.needs-you-fold.is-expanded {
  border-bottom-left-radius: 0;
  border-bottom-right-radius: 0;
}
.needs-you-fold-glyph {
  display: grid;
  place-items: center;
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  border-radius: 7px;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
}
.needs-you-fold-text {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex: 1;
  min-width: 0;
  margin: 0;
}
.needs-you-fold-title {
  flex-shrink: 0;
  color: var(--fg);
  font-weight: 600;
}
.needs-you-fold-names {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--fg3);
}
.needs-you-fold-btn {
  display: inline-flex;
  align-items: center;
  flex-shrink: 0;
  min-height: 32px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-weight: 500;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}
.needs-you-fold-btn:hover:not(:disabled) {
  background: color-mix(in srgb, var(--fg) 8%, transparent);
}
.needs-you-fold-btn--quiet {
  border-color: transparent;
  color: var(--fg2);
}
.needs-you-fold-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.needs-you-fold-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.needs-you-fold-error {
  flex-basis: 100%;
  margin: 0;
  color: var(--fg);
}
/* A folded card is a row of the fold's list, not a second box. */
.needs-you-stack :deep(.needs-you-card--folded) {
  margin: 0;
  border-top: 0;
  border-radius: 0;
}
.needs-you-stack :deep(.needs-you-card--folded:last-child) {
  margin-bottom: 8px;
  border-bottom-left-radius: var(--radius);
  border-bottom-right-radius: var(--radius);
}
@media (max-width: 560px) {
  .needs-you-fold-text { flex-basis: calc(100% - 34px); flex-direction: column; gap: 0; }
  .needs-you-fold-names { white-space: normal; }
}
@media (pointer: coarse) {
  .needs-you-fold-btn { min-height: var(--touch); }
}
</style>
