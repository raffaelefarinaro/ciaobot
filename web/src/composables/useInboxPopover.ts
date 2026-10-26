import { readonly, ref } from 'vue'
import type { Ref } from 'vue'

// Module-level on purpose: Home asks "open the inbox" without knowing which
// bell (sidebar or pane header) is on screen. Each bell watches the counter and
// the one that is rendered answers.
const requests = ref(0)

export function requestInboxOpen(): void {
  requests.value += 1
}

export function useInboxOpenRequests(): Readonly<Ref<number>> {
  return readonly(requests)
}
