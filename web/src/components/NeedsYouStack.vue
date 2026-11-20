<script setup lang="ts">
/** The chat's generic cards share a bounded, independently scrolling region. */
import { onMounted, ref, watch } from 'vue'
import NeedsYouCard from './NeedsYouCard.vue'
import type { ChatAction } from '../lib/types'

const props = defineProps<{ chatId: string; actions: ChatAction[] }>()
const stackEl = ref<HTMLElement | null>(null)

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
    .filter(c => ids.has(c.dataset.actionId ?? ''))
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

</script>

<template>
  <div v-if="actions.length" ref="stackEl" class="needs-you-stack">
    <NeedsYouCard v-for="action in actions" :key="action.id" :chat-id="chatId" :action="action" />
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
</style>
