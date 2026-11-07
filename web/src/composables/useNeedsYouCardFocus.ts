import { ref, watch } from 'vue'

// Keeps focus on the needs-you card when a control in the body is about to be
// disabled or removed as the action's state moves on. Bind `bodyEl` to the body root.
export function useNeedsYouCardFocus(state: () => string) {
  const bodyEl = ref<HTMLElement | null>(null)
  const focusCard = (): void =>
    (document.activeElement as HTMLElement | null)?.closest<HTMLElement>('.needs-you-card')?.focus()
  // Runs before the DOM is patched, so the control still holds the focus here.
  watch(state, () => { if (bodyEl.value?.contains(document.activeElement)) focusCard() })
  return { bodyEl, focusCard }
}
